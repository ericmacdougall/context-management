"""Make the waved-through variants of exp/disk_gate.py for the integ rules (one stated change each) and print, per
variant, the failing_first_variant.sh command line with the tests that must kill it. Run from writepath/.
usage: python3 integ_mutants.py <commit>"""
import pathlib
import sys

C = sys.argv[1]
G = pathlib.Path("exp/disk_gate.py").read_text()
T = "tests.test_disk_gate.Gate."
M = {
    "new_roles_unchecked": (
        "    for n in sorted((expected | legacy) & set(roles)):",
        '    for n in sorted((expected | legacy) & set(roles) - {"wp_sealer", "wp_rev1", "wp_rev2", "wp_reviewer"}):',
        ["test_integ_sealer_attribute_changed_blocks", "test_integ_reviewer_with_bypassrls_blocks",
         "test_integ_reviewer_group_that_can_log_in_blocks"]),
    "reviewer_members_waved": (
        "    for g, m in sorted(members - GP.MEMBERSHIPS - GP.LEGACY_MEMBERSHIPS):",
        '    for g, m in sorted(members - GP.MEMBERSHIPS - GP.LEGACY_MEMBERSHIPS - {x for x in members if x[0] == "wp_reviewer"}):',
        ["test_integ_extra_member_of_the_reviewer_group_blocks"]),
    "reviewer_membership_missing_waved": (
        "    for g, m in sorted(GP.MEMBERSHIPS - members) if strict else ():",
        '    for g, m in sorted(GP.MEMBERSHIPS - members - {("wp_reviewer", "wp_rev1"), ("wp_reviewer", "wp_rev2")}) if strict else ():',
        ["test_integ_reviewer_membership_missing_blocks"]),
    "group_settings_ignored": (
        '    have = {(r[0], r[1]): r[2] for r in cl["settings"]}',
        '    have = {(r[0], r[1]): r[2] for r in cl["settings"] if r[0] not in GP.GROUP_ROLES}',
        ["test_integ_setting_on_the_reviewer_group_blocks"]),
    "sealer_settings_waved": (
        "        elif conf != want:",
        '        elif conf != want and r != "wp_sealer":',
        ["test_integ_sealer_setting_changed_during_the_run_voids"]),
    "connect_waved": (
        "        for g in sorted(conn - set(GP.CONNECT[k]) - {owner, admin}):",
        '        for g in sorted(conn - set(GP.CONNECT[k]) - {owner, admin, "wp_reviewer", "wp_sealer"}):',
        ["test_integ_reviewer_group_connect_blocks", "test_integ_sealer_connect_to_the_key_store_blocks"]),
    "sealer_connect_missing_waved": (
        "        for g in sorted(set(GP.CONNECT[k]) - conn) if strict else ():",
        '        for g in sorted(set(GP.CONNECT[k]) - conn - {"wp_sealer"}) if strict else ():',
        ["test_integ_sealer_connect_missing_blocks"]),
    "temporary_unchecked": (
        '        for g in sorted({g for g, p in acl if p == "TEMPORARY"} - set(GP.TEMPORARY[k]) - {owner, admin}):',
        "        for g in ():",
        ["test_integ_public_temporary_on_a_stream_database_blocks"]),
    "app_grants_on_new_tables_waved": (
        "    for t in sorted(set(tables) & set(GP.POLICY)):",
        '    for t in sorted(set(tables) & set(GP.POLICY) - {"write_refusals", "identifier_proposals"}):',
        ["test_integ_app_grant_on_a_hardening_table_blocks"]),
    "new_table_triggers_not_always_waved": (
        "        if en != \"A\":\n            why.append(f\"G3 trigger {t}.{name} is enabled {en!r}, not ALWAYS\")",
        "        if en != \"A\" and t not in (\"identifier_proposals\", \"quarantine_removed\", \"write_refusals\"):\n"
        "            why.append(f\"G3 trigger {t}.{name} is enabled {en!r}, not ALWAYS\")",
        ["test_integ_trigger_disabled_on_identifier_proposals_during_the_run_voids",
         "test_integ_trigger_disabled_on_quarantine_removed_blocks", "test_integ_trigger_on_write_refusals_not_always_blocks"]),
    "exemption_widened": (
        "    for t in sorted(set(tables) - set(GP.UNGUARDED_OK)):",
        '    for t in sorted(set(tables) - set(GP.UNGUARDED_OK) - {"identifier_proposals", "quarantine_removed"}):',
        ["test_integ_identifier_proposals_without_its_guard_blocks"]),
    "exemption_writer_unchecked": (
        '        if t in tables and (f is None or f[2] != GP.EVENTS_OWNER or f[3] != "True"):',
        "        if False:",
        ["test_integ_write_refusals_writer_not_security_definer_blocks"]),
    "namespaces_unchecked": (
        "        for n in sorted(set(have) - set(want)):",
        "        for n in ():",
        ["test_integ_schema_outside_the_committed_set_blocks"]),
    "seal_relations_unchecked": (
        "    for n in sorted(rel):",
        "    for n in ():",
        ["test_integ_extra_relation_in_the_seal_schema_during_the_run_voids"]),
    "seal_functions_unchecked": (
        "    for n in sorted(sf):",
        "    for n in ():",
        ["test_integ_seal_function_as_security_definer_blocks"]),
    "seal_triggers_unchecked": (
        '    for t, name, _typ, en, _fn in ev["seal_triggers"]:',
        "    for t, name, _typ, en, _fn in []:",
        ["test_integ_trigger_disabled_on_seal_keys_blocks"]),
    "role_table_privs_unchecked": (
        "    for r in sorted(set(have) | set(want)):",
        "    for r in ():",
        ["test_integ_extra_grant_to_the_sealer_during_the_run_voids", "test_integ_reviewer_insert_on_proposals_blocks",
         "test_integ_reviewer_read_missing_blocks", "test_integ_seal_keys_readable_blocks"]),
    "schema_privs_unchecked": (
        "    for (n, p), w in sorted(GP.SCHEMA_PRIVS.items()):",
        "    for (n, p), w in ():",
        ["test_integ_usage_on_the_seal_schema_blocks", "test_integ_create_on_public_during_the_run_voids"]),
    "execute_unchecked": (
        "    for f, w in sorted(GP.EXECUTE_ONLY.items()):",
        "    for f, w in ():",
        ["test_integ_app_may_approve_identifiers_blocks", "test_integ_seal_function_executable_during_the_run_voids"]),
    "seal_identity_unchecked": (
        '        sa, sn = ([r[1] for r in s[db]["schema"] + s[db].get("seal_schema", [])] for s in (adm, now))',
        '        sa, sn = ([r[1] for r in s[db]["schema"]] for s in (adm, now))',
        ["test_integ_seal_schema_replaced_during_the_run_voids"]),
    "stream_containers_ignored": (
        "    for n in stream or []:",
        "    for n in []:",
        ["test_integ_key_service_container_before_the_run_blocks", "test_integ_stream_labelled_container_before_the_run_blocks",
         "test_integ_service_container_left_at_the_end_voids"]),
    "stream_label_ignored": (
        "        if name.startswith(config.OWN_PREFIX) or (isinstance(labels, dict) and labels.get(key) == value):",
        "        if name.startswith(config.OWN_PREFIX):",
        ["test_integ_stream_labelled_container_before_the_run_blocks"]),
    "gf1_stop_line_moved": (
        "START_GB, STOP_GB, CAP_GB = 10.0, 8.0, 2.0",
        "START_GB, STOP_GB, CAP_GB = 10.0, 8.5, 2.0",
        ["test_integ_gf1_lines_equal_h6d"]),
}
d = pathlib.Path("results/failing-first/variants")
for name, (a, b, tests) in M.items():
    assert G.count(a) == 1, (name, G.count(a))
    v = d / f"disk_gate.integ-mutant-{name}.py"
    v.write_text(G.replace(a, b))
    out = f"results/failing-first/integ/integ-mutant-{name}.red.out"
    print(f'sh runkit/failing_first_variant.sh {C} {v} DISK_GATE_UNDER_TEST "{" ".join(T + t for t in tests)}" {out} '
          "exp/disk_gate.py")
