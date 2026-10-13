# -*- coding: utf-8 -*-
"""IDE ownership, recovery, bounded reads and Crush contract; fully isolated."""
import hashlib
import json
import os
import shutil
import sys
import unittest
from unittest import mock

import isolate

isolate.bootstrap()

from wbrelay import ides, state, settings
from wbrelay.atomicio import write_bytes, write_text
from wbrelay.ides import base, codex, crush, dsh, opencode
from wbrelay.web.api_state import ApiStateMixin


PORT = isolate.port()
URL = "http://127.0.0.1:%d/v1" % PORT
MODEL = {"demo-model": {"id": "demo-model", "context_window": 12000,
                         "max_output_tokens": 1500, "reasoning_efforts": ["low", "high"]},
         "disabled-model": {"id": "disabled-model"}}
CREDS = 'version: 1\r\nrecords:\r\n  account:\r\n    payload: FAKE-RECORD-NOT-FOR-BACKUP\r\n'
PATCH = ('- id: llm-pi-ai\r\n  config:\r\n    providers: {}\r\n'
         '- id: agent-default-model\r\n  config:\r\n    provider: old\r\n    model: old-model\r\n')


def digest(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Response(ApiStateMixin):
    body = {"enabled": True}

    def _json(self, status, payload):
        return status, payload

    def _err(self, status, message, *args):
        return status, message

    def _payload(self):
        return self.body


class TakeoverTests(unittest.TestCase):
    def setUp(self):
        isolate.assert_isolated()
        state._SERVER_PORT = PORT
        self.patch_path = os.path.join(dsh.DSH_PROFILES, "desktop", dsh.PATCH_NAME)
        for folder in (base.IDE_BACKUP_DIR, codex.CODEX_HOME, opencode.OPENCODE_HOME,
                       dsh.DSH_HOME, crush.CRUSH_HOME):
            self.assertTrue(os.path.abspath(folder).startswith(os.path.abspath(isolate.tmp_root()) + os.sep))
            if os.path.isdir(folder):
                shutil.rmtree(folder)
        self.mocks = []
        for module in (crush, dsh, opencode):
            for name, value in (("_collect_models_for_ide", lambda: MODEL),
                                ("_route_models_set", lambda: {"demo-model"})):
                p = mock.patch.object(module, name, value)
                p.start()
                self.mocks.append(p)
        isolate.write_settings(isolate.base_settings())
        settings._settings_cache.update(mtime=None, data=None)

    def tearDown(self):
        for p in reversed(self.mocks):
            p.stop()
        isolate.assert_isolated()

    def json_config(self, path, obj):
        write_text(path, json.dumps(obj))

    def dsh_fixture(self):
        write_text(self.patch_path, PATCH)
        write_text(dsh.DSH_CREDENTIALS_PATH, CREDS)

    def owner_case(self, ide_id, url):
        if ide_id == "codex":
            write_text(codex.CODEX_CONFIG_PATH, 'model_provider = "active"\n'
                       '[model_providers.first]\nbase_url = "https://example.invalid/v1"\n'
                       '[model_providers.active]\nbase_url = %s\n' % json.dumps(url))
        elif ide_id == "opencode":
            self.json_config(opencode.OPENCODE_CONFIG_PATH,
                             {"provider": {"wbrelay": {"options": {"baseURL": url}}}})
        elif ide_id == "crush":
            self.json_config(crush.CRUSH_CONFIG_PATH, {"providers": {"wbrelay": {"base_url": url}}})
        else:
            self.dsh_fixture()
            raw = dsh._build_patch(PATCH, PORT)[0].replace(URL, url)
            write_text(self.patch_path, raw)
        return ides.ide_adapter(ide_id)["owner"]()

    def test_four_state_ownership_and_no_credentials(self):
        for ide_id in ("codex", "opencode", "dsh", "crush"):
            adp = ides.ide_adapter(ide_id)
            self.assertEqual(adp["owner"]()["state"], "none")
            for url, expected in ((URL, "wbrelay"),
                                  ("http://127.0.0.1:18799/v1", "wbrelay_stale"),
                                  ("https://demo:FAKE-URL-PASSWORD@example.invalid/v1?api_key=FAKE-QUERY-SECRET", "external")):
                with self.subTest(ide=ide_id, url=url):
                    owner = self.owner_case(ide_id, url)
                    self.assertEqual(owner["state"], expected)
                    self.assertNotIn("FAKE-", json.dumps(owner))
                    self.assertEqual(set(owner), {"state", "label", "detail", "port", "source"})

    def test_selected_external_provider_wins_over_residual_marker(self):
        for module, path, cfg in (
                (opencode, opencode.OPENCODE_CONFIG_PATH,
                 {"model": "external/demo", "provider": {"wbrelay": {"options": {"baseURL": URL}}}}),
                (crush, crush.CRUSH_CONFIG_PATH,
                 {"models": {"large": {"provider": "external"}}, "providers": {"wbrelay": {"base_url": URL}}})):
            self.json_config(path, cfg)
            fn = getattr(module, "_" + module.__name__.split(".")[-1] + "_owner")
            self.assertEqual(fn()["state"], "external")
        self.dsh_fixture()
        raw = dsh._build_patch(PATCH, PORT)[0].replace("provider: wbrelay", "provider: external")
        write_text(self.patch_path, raw)
        self.assertEqual(dsh._dsh_owner()["state"], "external")

    def test_owner_parse_failures_are_none(self):
        for path, fn in ((codex.CODEX_CONFIG_PATH, codex._codex_owner),
                         (opencode.OPENCODE_CONFIG_PATH, opencode._opencode_owner),
                         (crush.CRUSH_CONFIG_PATH, crush._crush_owner),
                         (self.patch_path, dsh._dsh_owner)):
            for raw in (b'\xff', b'{broken'):
                write_bytes(path, raw)
                self.assertEqual(fn()["state"], "none")

    def test_loopback_endpoints(self):
        for host in ("localhost", "[::1]", "127.0.0.2"):
            self.assertEqual(base.ide_owner("http://%s:%d/v1/" % (host, PORT))["state"], "wbrelay")
        self.assertEqual(base.ide_owner("http://127.0.0.1:%d/other" % PORT)["state"], "external")

    def test_installation_probes(self):
        for ide_id, module, home in (("codex", codex, codex.CODEX_HOME),
                                    ("opencode", opencode, opencode.OPENCODE_HOME),
                                    ("dsh", dsh, dsh.DSH_HOME), ("crush", crush, crush.CRUSH_HOME)):
            detect = ides.ide_adapter(ide_id)["detect"]
            with mock.patch.object(module.shutil, "which", return_value=None):
                self.assertFalse(detect())
                os.makedirs(home)
                self.assertTrue(detect())
                os.rmdir(home)
            with mock.patch.object(module.shutil, "which", return_value="fake-client"):
                self.assertTrue(detect())

    def test_codex_scoped_injection_and_complete_byte_restore(self):
        original = ('\ufeff# original\r\nmodel_provider = "active.name"\r\nmodel_catalog_json = "old.json"\r\n'
                    '[model_providers.first]\r\nname = "first"\r\nbase_url = "https://example.invalid/first"\r\n'
                    'experimental_bearer_token = "FAKE-FIRST-TOKEN"\r\n'
                    '[model_providers."active.name"]\r\nname = "old"\r\nbase_url = "https://example.invalid/active"\r\n'
                    '[mcp_servers.demo]\r\nbase_url = "https://example.invalid/mcp"\r\n').encode("utf-8")
        write_bytes(codex.CODEX_CONFIG_PATH, original)
        untouched = original.decode("utf-8").split('[model_providers.first]', 1)[1].split('[model_providers.', 1)[0]
        self.assertTrue(codex._codex_backup())
        self.assertTrue(codex._codex_inject(PORT))
        injected = base.read_ide_text(codex.CODEX_CONFIG_PATH)
        self.assertIn(untouched, injected)
        self.assertIn('base_url = "https://example.invalid/mcp"', injected)
        self.assertEqual(codex._codex_owner()["state"], "wbrelay")
        self.assertIn('model_catalog_json = ', injected.split('[model_providers.', 1)[0])
        bak = base._ide_read_backup("codex")
        self.assertEqual(bak["raw"].encode("utf-8"), original)
        self.assertTrue(codex._codex_backup())
        self.assertTrue(codex._codex_inject(PORT))
        self.assertEqual(bak["raw"], base._ide_read_backup("codex")["raw"])
        self.assertTrue(codex._codex_restore())
        with open(codex.CODEX_CONFIG_PATH, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertFalse(base.ide_backup_exists("codex"))

    def test_codex_no_target_segment_and_guard(self):
        original = '# only config\n[mcp_servers.demo]\ncommand = "demo"\n'
        write_text(codex.CODEX_CONFIG_PATH, original)
        self.assertTrue(codex._codex_backup())
        self.assertTrue(codex._codex_inject(PORT))
        text = base.read_ide_text(codex.CODEX_CONFIG_PATH)
        self.assertEqual(codex._codex_owner()["state"], "wbrelay")
        external = text.replace(URL, "https://example.invalid/v1")
        write_text(codex.CODEX_CONFIG_PATH, external)
        self.assertFalse(codex._codex_restore())
        self.assertTrue(base.ide_backup_exists("codex"))
        self.assertEqual(base.read_ide_text(codex.CODEX_CONFIG_PATH), external)

    def test_absent_codex_and_json_configs_removed_on_restore(self):
        for ide_id in ("codex", "opencode", "crush"):
            adp = ides.ide_adapter(ide_id)
            self.assertTrue(adp["backup"]())
            self.assertTrue(adp["inject"](PORT))
            self.assertTrue(adp["restore"]())
            self.assertFalse(os.path.exists(adp["config_paths"]()[0]))
            self.assertFalse(base.ide_backup_exists(ide_id))

    def test_crush_schema_selection_filter_sync_and_restore(self):
        cfg = {"providers": {"other": {"type": "openai-compat", "base_url": "https://example.invalid/v1"}},
               "models": {"large": {"provider": "other", "model": "old"}}, "options": {"debug": True}}
        original = ('\ufeff' + json.dumps(cfg, indent=2).replace("\n", "\r\n")).encode("utf-8")
        write_bytes(crush.CRUSH_CONFIG_PATH, original)
        self.assertTrue(crush._crush_backup())
        self.assertTrue(crush._crush_inject(PORT))
        _raw, new = base.read_ide_json(crush.CRUSH_CONFIG_PATH)
        provider = new["providers"]["wbrelay"]
        self.assertEqual(provider["type"], "openai-compat")
        self.assertEqual(provider["base_url"], URL)
        self.assertEqual(provider["models"][0]["context_window"], 12000)
        self.assertEqual(provider["models"][0]["default_max_tokens"], 1500)
        self.assertEqual(provider["models"][0]["reasoning_levels"], ["low", "high"])
        self.assertEqual(len(provider["models"]), 1)
        self.assertEqual(new["providers"]["other"], cfg["providers"]["other"])
        self.assertEqual(new["models"]["large"], {"provider": "wbrelay", "model": "demo-model"})
        before = os.stat(crush.CRUSH_CONFIG_PATH).st_mtime_ns
        self.assertEqual(crush._crush_sync_catalog(), (1, 1))
        self.assertEqual(before, os.stat(crush.CRUSH_CONFIG_PATH).st_mtime_ns)
        self.assertFalse(ides.ide_adapter("crush")["restartable"])
        self.assertTrue(crush._crush_backup())
        self.assertEqual(base._ide_read_backup("crush")["raw"].encode("utf-8"), original)
        self.assertTrue(crush._crush_restore())
        with open(crush.CRUSH_CONFIG_PATH, "rb") as fh:
            self.assertEqual(fh.read(), original)

    def test_crush_path_environment_matches_global_data(self):
        with mock.patch.dict(os.environ, {"CRUSH_GLOBAL_DATA": "", "XDG_DATA_HOME": ""}):
            self.assertEqual(crush._crush_home(), os.path.join(os.environ["LOCALAPPDATA"], "crush"))
        path = os.path.join(isolate.tmp_root(), "explicit-data")
        with mock.patch.dict(os.environ, {"CRUSH_GLOBAL_DATA": path, "XDG_DATA_HOME": "ignored"}):
            self.assertEqual(crush._crush_home(), path)
        with mock.patch.dict(os.environ, {"CRUSH_GLOBAL_DATA": "", "XDG_DATA_HOME": path}):
            self.assertEqual(crush._crush_home(), os.path.join(path, "crush"))

    def test_each_applied_hash_tracks_external_changes(self):
        for ide_id in ("codex", "opencode", "dsh", "crush"):
            adp = ides.ide_adapter(ide_id)
            if ide_id == "dsh":
                self.dsh_fixture()
            self.assertTrue(adp["backup"]())
            self.assertTrue(adp["inject"](PORT))
            status = base.ide_backup_status(ide_id)
            self.assertTrue(status["backup_exists"])
            self.assertTrue(status["applied_at"])
            self.assertFalse(status["external_change"])
            path = adp["config_paths"]()[0]
            original = base.read_ide_text(path)
            write_text(path, original + "\n")
            self.assertTrue(base.ide_backup_status(ide_id)["external_change"])
            write_text(path, original)
            self.assertFalse(base.ide_backup_status(ide_id)["external_change"])
        self.assertNotIn("FAKE-RECORD-NOT-FOR-BACKUP", json.dumps(base._ide_read_backup("dsh")))

    def test_api_contract_reads_disk_in_readonly_without_writes(self):
        self.json_config(crush.CRUSH_CONFIG_PATH, {"providers": {"wbrelay": {"base_url": URL,
                                                                            "api_key": "FAKE-API-SECRET"}}})
        files = {p: digest(p) for p in (crush.CRUSH_CONFIG_PATH, settings.SETTINGS_PATH)}
        with mock.patch.object(base, "READONLY", True):
            code, data = Response()._api_ides()
            self.assertEqual(code, 200)
            self.assertEqual(len(data["ides"]), 4)
            item = next(i for i in data["ides"] if i["id"] == "crush")
            self.assertEqual(item["owner"]["state"], "wbrelay")
            self.assertFalse(item["enabled"])
            self.assertEqual(set(item), {"id", "name", "detected", "enabled", "restartable", "config_paths",
                                         "backup_exists", "applied_at", "external_change", "owner"})
            self.assertNotIn("FAKE-API-SECRET", json.dumps(data))
            self.assertFalse(base.ide_backup_exists("crush"))
            self.assertEqual(files, {p: digest(p) for p in files})

    def test_large_and_invalid_utf8_inputs_rejected_without_writes(self):
        for ide_id in ("codex", "opencode", "dsh", "crush"):
            adp = ides.ide_adapter(ide_id)
            path = self.patch_path if ide_id == "dsh" else adp["config_paths"]()[0]
            for raw in (b'\xff', b'x' * (base.MAX_CONFIG_BYTES + 1)):
                with self.subTest(ide=ide_id, bytes=len(raw)):
                    write_bytes(path, raw)
                    original = digest(path)
                    self.assertFalse(adp["backup"]())
                    self.assertFalse(adp["inject"](PORT))
                    self.assertEqual(adp["sync_catalog"](), (0, 0))
                    self.assertEqual(digest(path), original)
                    self.assertFalse(base.ide_backup_exists(ide_id))

    def test_dsh_crlf_restore_and_credentials_minimal_snapshot(self):
        self.dsh_fixture()
        old = (digest(self.patch_path), digest(dsh.DSH_CREDENTIALS_PATH))
        self.assertTrue(dsh._dsh_backup())
        self.assertTrue(dsh._dsh_inject(PORT))
        self.assertNotIn("FAKE-RECORD-NOT-FOR-BACKUP", json.dumps(base._ide_read_backup("dsh")))
        self.assertTrue(dsh._dsh_restore())
        self.assertEqual(old, (digest(self.patch_path), digest(dsh.DSH_CREDENTIALS_PATH)))

    def test_readonly_primitives_keep_all_files_and_backups(self):
        self.dsh_fixture()
        for ide_id in ("codex", "opencode", "dsh", "crush"):
            adp = ides.ide_adapter(ide_id)
            self.assertTrue(adp["backup"]())
            self.assertTrue(adp["inject"](PORT))
            module = {"codex": codex, "opencode": opencode, "dsh": dsh, "crush": crush}[ide_id]
            paths = adp["config_paths"]() + [base.ide_backup_path(ide_id)]
            original = {p: digest(p) for p in paths}
            with mock.patch.object(module, "READONLY", True), mock.patch.object(base, "READONLY", True):
                self.assertFalse(adp["backup"]())
                self.assertFalse(adp["inject"](PORT))
                self.assertEqual(adp["sync_catalog"](), (0, 0))
                self.assertFalse(adp["restore"]())
            self.assertEqual(original, {p: digest(p) for p in paths})

    def test_three_gates_and_crush_no_restart(self):
        self.assertTrue(base.NO_IDE_INJECT)
        with mock.patch.object(codex, "_write_codex_config", side_effect=AssertionError("must not write")):
            ides.ides_backup_and_inject(PORT)
            ides.codex_backup_and_switch(PORT)
            ides.codex_restore()
            self.assertEqual(Response()._api_ide_toggle("crush")[0], 403)
        with mock.patch.object(base, "NO_IDE_INJECT", False):
            self.assertFalse(base.ide_write_allowed("crush"))
            self.assertTrue(base.set_ide_enabled("crush", True))
            self.assertTrue(base.ide_write_allowed("crush"))
            self.assertEqual(Response()._api_ide_restart("crush")[0], 400)
            with mock.patch.object(crush.shutil, "which", return_value=None):
                self.assertEqual(Response()._api_ide_toggle("crush")[0], 400)

    def test_failed_restore_preserves_snapshot(self):
        for ide_id, module, writer in (("codex", codex, "_write_codex_config"),
                                      ("opencode", opencode, "_opencode_write"),
                                      ("crush", crush, "_crush_write")):
            adp = ides.ide_adapter(ide_id)
            self.owner_case(ide_id, "https://example.invalid/v1")
            self.assertTrue(adp["backup"]())
            self.assertTrue(adp["inject"](PORT))
            with mock.patch.object(module, writer, return_value=False):
                self.assertFalse(adp["restore"]())
            self.assertTrue(base.ide_backup_exists(ide_id))

    def test_corrupt_recovery_snapshots_are_never_replaced(self):
        self.dsh_fixture()
        for ide_id in ("codex", "opencode", "dsh", "crush"):
            path = base.ide_backup_path(ide_id)
            write_bytes(path, b'{broken-FAKE-snapshot')
            before = digest(path)
            self.assertFalse(ides.ide_adapter(ide_id)["backup"]())
            self.assertEqual(before, digest(path))

    def test_failed_toggle_is_not_reported_as_success(self):
        adp = ides.ide_adapter("crush")
        self.json_config(crush.CRUSH_CONFIG_PATH, {})
        with mock.patch.object(base, "NO_IDE_INJECT", False), mock.patch.dict(adp, {"inject": lambda port: False}):
            status, _message = Response()._api_ide_toggle("crush")
            self.assertEqual(status, 500)
            self.assertFalse(base.ide_enabled("crush"))
            self.assertTrue(base.ide_backup_exists("crush"))

    def test_successful_catalog_sync_updates_applied_hash(self):
        for ide_id, module in (("opencode", opencode), ("dsh", dsh), ("crush", crush)):
            adp = ides.ide_adapter(ide_id)
            if ide_id == "dsh":
                self.dsh_fixture()
            self.assertTrue(adp["backup"]())
            self.assertTrue(adp["inject"](PORT))
            with mock.patch.object(module, "_collect_models_for_ide", return_value={"new-model": {"id": "new-model"}}), \
                    mock.patch.object(module, "_route_models_set", return_value={"new-model"}):
                self.assertEqual(adp["sync_catalog"](), (1, 0))
            self.assertFalse(base.ide_backup_status(ide_id)["external_change"])

    def test_dsh_credentials_reads_are_also_bounded_and_strict(self):
        write_text(self.patch_path, PATCH)
        for raw in (b'\xff', b'x' * (base.MAX_CONFIG_BYTES + 1)):
            write_bytes(dsh.DSH_CREDENTIALS_PATH, raw)
            before = (digest(self.patch_path), digest(dsh.DSH_CREDENTIALS_PATH))
            self.assertFalse(dsh._dsh_backup())
            self.assertFalse(dsh._dsh_inject(PORT))
            self.assertEqual(before, (digest(self.patch_path), digest(dsh.DSH_CREDENTIALS_PATH)))
            self.assertFalse(base.ide_backup_exists("dsh"))

    def test_dsh_existing_ref_preserves_literal_quoting_and_comment(self):
        self.dsh_fixture()
        cred = "version: 1\r\nrefs:\r\n  WBRELAY_API_KEY: 'FAKE-old-ref' # keep\r\nrecords:\r\n  account: FAKE-token\r\n"
        write_text(dsh.DSH_CREDENTIALS_PATH, cred)
        self.assertTrue(dsh._dsh_backup())
        self.assertTrue(dsh._dsh_inject(PORT))
        bak = base._ide_read_backup("dsh")
        self.assertNotIn("FAKE-token", json.dumps(bak))
        self.assertTrue(dsh._dsh_restore())
        self.assertEqual(base.read_ide_text(dsh.DSH_CREDENTIALS_PATH), cred)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TakeoverTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
