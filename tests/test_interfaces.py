"""The network adapter check (#234): what a role is, how it is found among the
laptop's adapters, and when a missing one raises.

No test here runs ifconfig or ip. The runner is a fake, and the listings are
fixtures with documentation MACs and addresses only.
"""

import asyncio
import json
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tacet import faults, interfaces, reach
from tacet.interfaces import Expected, Found, Role, Status
from tests.reach_fixtures import ManualClock

FIXTURES = Path(__file__).parent / "fixtures"
MAC_TEXT = (FIXTURES / "ifconfig-macos.txt").read_text(encoding="utf-8")
MAC_CABLE_OUT = (FIXTURES / "ifconfig-macos-en7-cable-out.txt").read_text(encoding="utf-8")
MAC_DEV = (FIXTURES / "ifconfig-macos-dev.txt").read_text(encoding="utf-8")
LINUX_TEXT = (FIXTURES / "ip-addr-linux.json").read_text(encoding="utf-8")

DANTE_MAC = "00:00:5e:00:53:02"
CONTROL_MAC = "00:00:5e:00:53:01"
SCORING_TAG = 20

EXPECTED = {
    Role.DANTE: Expected(DANTE_MAC),
    Role.CONTROL: Expected(CONTROL_MAC),
    Role.SCORING: Expected(CONTROL_MAC, SCORING_TAG),
}

MAC_TOOLS = interfaces.Tools(platform=reach.Platform.MACOS, ifconfig="/sbin/ifconfig", ip=None)
LINUX_TOOLS = interfaces.Tools(platform=reach.Platform.LINUX, ifconfig=None, ip="/sbin/ip")


def by_name(adapters, name):
    return next(a for a in adapters if a.name == name)


def adapter(name, mac, *, up=True, active=True, ipv4=("198.51.100.5",), **kwargs):
    return interfaces.Adapter(name=name, mac=mac, up=up, active=active, ipv4=tuple(ipv4), **kwargs)


def without(adapters, *names):
    return tuple(a for a in adapters if a.name not in names)


class TestCanonicalMac(unittest.TestCase):
    def test_padded_and_unpadded_spellings_agree(self):
        self.assertEqual(interfaces.canonical_mac("00:00:5e:00:53:02"), "00:00:5e:00:53:02")
        self.assertEqual(interfaces.canonical_mac("0:0:5e:0:53:2"), "00:00:5e:00:53:02")

    def test_case_and_dashes_are_normalised(self):
        self.assertEqual(interfaces.canonical_mac("00-00-5E-00-53-AB"), "00:00:5e:00:53:ab")

    def test_wrong_shapes_are_refused(self):
        for bad in ("00:00:5e:00:53", "00:00:5e:00:53:02:03", "000:00:5e:00:53:02", "gg:00:5e:00:53:02", "", "  "):
            self.assertIsNone(interfaces.canonical_mac(bad), repr(bad))


class TestParseIfconfig(unittest.TestCase):
    adapters: tuple[interfaces.Adapter, ...]

    @classmethod
    def setUpClass(cls):
        cls.adapters = interfaces.parse_ifconfig(MAC_TEXT)

    def test_an_adapter_without_a_mac_is_skipped(self):
        names = {a.name for a in self.adapters}
        self.assertFalse(names & {"lo0", "gif0", "stf0", "utun0"})

    def test_en8_is_read_and_its_inet6_is_not_an_address(self):
        en8 = by_name(self.adapters, "en8")
        self.assertEqual(en8.mac, "00:00:5e:00:53:02")
        self.assertEqual(en8.ipv4, ("169.254.33.7",))
        self.assertTrue(en8.link_up)

    def test_the_bridge_is_flagged_and_its_configuration_ids_are_not_read(self):
        bridge = by_name(self.adapters, "bridge0")
        self.assertTrue(bridge.bridge)
        self.assertEqual(bridge.mac, "00:00:5e:00:53:11")
        # `id 0:0:0:0:0:0` sits under two tabs; only vlan1's own ether line is that MAC.
        self.assertEqual([a.name for a in self.adapters if a.mac == "00:00:00:00:00:00"], ["vlan1"])
        self.assertFalse(by_name(self.adapters, "en1").bridge)

    def test_a_cable_out_is_link_down(self):
        self.assertFalse(by_name(self.adapters, "en9").link_up)

    def test_an_adapter_that_is_not_up_has_no_link_even_when_status_says_active(self):
        en10 = by_name(self.adapters, "en10")
        self.assertTrue(en10.active)
        self.assertFalse(en10.up)
        self.assertFalse(en10.link_up)

    def test_running_is_not_a_link(self):
        # en1's flags say RUNNING; its status line says inactive.
        self.assertIn("RUNNING", "".join(line for line in MAC_TEXT.splitlines() if line.startswith("en1:")))
        self.assertFalse(by_name(self.adapters, "en1").link_up)

    def test_a_vlan_interface_names_its_tag_and_parent(self):
        vlan0 = by_name(self.adapters, "vlan0")
        self.assertEqual((vlan0.vlan, vlan0.parent), (20, "en7"))
        self.assertEqual(vlan0.mac, by_name(self.adapters, "en7").mac)
        self.assertIsNone(by_name(self.adapters, "en7").vlan)

    def test_a_vlan_with_no_parent_has_none(self):
        vlan1 = by_name(self.adapters, "vlan1")
        self.assertEqual(vlan1.vlan, 30)
        self.assertIsNone(vlan1.parent)

    def test_ipv4_with_its_netmask_is_read_once(self):
        self.assertEqual(by_name(self.adapters, "en7").ipv4, ("198.51.100.123",))
        self.assertEqual(by_name(self.adapters, "vlan0").ipv4, ("203.0.113.50",))

    def test_text_with_no_headers_raises(self):
        with self.assertRaises(interfaces.ParseError):
            interfaces.parse_ifconfig("ifconfig: not what you expected\n")
        with self.assertRaises(interfaces.ParseError):
            interfaces.parse_ifconfig("")

    def test_this_macs_own_capture_parses(self):
        adapters = interfaces.parse_ifconfig(MAC_DEV)
        self.assertTrue(any(a.link_up for a in adapters))
        vlan0 = by_name(adapters, "vlan0")
        self.assertEqual((vlan0.vlan, vlan0.parent), (4, "en7"))
        self.assertTrue(vlan0.link_up)
        self.assertTrue(by_name(adapters, "bridge0").bridge)
        # awdl0 and llw0 share one MAC, which is what the lookup has to survive.
        self.assertEqual(by_name(adapters, "awdl0").mac, by_name(adapters, "llw0").mac)

    def test_the_dev_capture_holds_no_real_addresses(self):
        for line in MAC_DEV.splitlines():
            if line.startswith("\tether "):
                self.assertTrue(line.startswith("\tether 00:00:5e:00:53:"), line)


class TestParseIpJson(unittest.TestCase):
    adapters: tuple[interfaces.Adapter, ...]

    @classmethod
    def setUpClass(cls):
        cls.adapters = interfaces.parse_ip_json(LINUX_TEXT)

    def test_an_ordinary_adapter(self):
        en = by_name(self.adapters, "enp3s0")
        self.assertEqual(en.mac, "00:00:5e:00:53:01")
        self.assertEqual(en.ipv4, ("198.51.100.124",))
        self.assertTrue(en.link_up)
        self.assertIsNone(en.vlan)

    def test_no_carrier_is_link_down(self):
        en = by_name(self.adapters, "enx00005e005302")
        self.assertTrue(en.up)
        self.assertFalse(en.active)
        self.assertFalse(en.link_up)
        self.assertEqual(en.ipv4, ())

    def test_a_vlan_reads_its_id_and_parent_from_linkinfo_and_link(self):
        vlan = by_name(self.adapters, "enp3s0.20")
        self.assertEqual((vlan.vlan, vlan.parent), (20, "enp3s0"))

    def test_a_bridge_is_flagged(self):
        self.assertTrue(by_name(self.adapters, "br0").bridge)
        self.assertFalse(by_name(self.adapters, "eth1").bridge)

    def test_inet6_is_not_an_address(self):
        self.assertEqual(by_name(self.adapters, "enp3s0").ipv4, ("198.51.100.124",))

    def test_bad_json_and_bad_shapes_raise(self):
        for bad in ("not json", "{}", "[1]", json.dumps([{"address": "00:00:5e:00:53:01"}])):
            with self.assertRaises(interfaces.ParseError, msg=bad):
                interfaces.parse_ip_json(bad)


class TestFind(unittest.TestCase):
    adapters: tuple[interfaces.Adapter, ...]

    @classmethod
    def setUpClass(cls):
        cls.adapters = interfaces.parse_ifconfig(MAC_TEXT)

    def find(self, role, expected, adapters=None):
        return interfaces.find(role, expected, self.adapters if adapters is None else adapters)

    def test_a_present_adapter(self):
        found = self.find(Role.DANTE, Expected(DANTE_MAC))
        self.assertEqual((found.status, found.name), (Status.PRESENT, "en8"))

    def test_a_replugged_adapter_is_found_under_its_new_name(self):
        before = (adapter("en7", CONTROL_MAC),)
        after = (adapter("en9", CONTROL_MAC),)
        self.assertEqual(self.find(Role.CONTROL, Expected(CONTROL_MAC), before).name, "en7")
        found = self.find(Role.CONTROL, Expected(CONTROL_MAC), after)
        self.assertEqual((found.status, found.name), (Status.PRESENT, "en9"))

    def test_an_adapter_that_is_not_there_is_missing(self):
        found = self.find(Role.DANTE, Expected("00:00:5e:00:53:99"))
        self.assertEqual((found.status, found.name, found.ipv4), (Status.MISSING, None, ()))

    def test_a_cable_out_is_link_down(self):
        found = self.find(Role.DANTE, Expected("00:00:5e:00:53:03"))
        self.assertEqual((found.status, found.name), (Status.LINK_DOWN, "en9"))

    def test_control_with_a_link_and_no_ipv4_has_no_address(self):
        listing = (adapter("en7", CONTROL_MAC, ipv4=()),)
        self.assertEqual(self.find(Role.CONTROL, Expected(CONTROL_MAC), listing).status, Status.NO_ADDRESS)

    def test_control_with_only_a_link_local_address_has_no_address(self):
        listing = (adapter("en7", CONTROL_MAC, ipv4=("169.254.1.1",)),)
        self.assertEqual(self.find(Role.CONTROL, Expected(CONTROL_MAC), listing).status, Status.NO_ADDRESS)

    def test_dante_with_only_a_link_local_address_is_present(self):
        # en8 in the fixture is exactly this.
        self.assertEqual(by_name(self.adapters, "en8").ipv4, ("169.254.33.7",))
        self.assertEqual(self.find(Role.DANTE, Expected(DANTE_MAC)).status, Status.PRESENT)

    def test_a_real_address_beside_a_link_local_one_counts(self):
        listing = (adapter("en7", CONTROL_MAC, ipv4=("169.254.1.1", "198.51.100.9")),)
        found = self.find(Role.CONTROL, Expected(CONTROL_MAC), listing)
        self.assertEqual((found.status, found.address), (Status.PRESENT, "198.51.100.9"))

    def test_an_active_candidate_beats_an_inactive_one(self):
        listing = (
            adapter("en3", DANTE_MAC, active=False),
            adapter("en4", DANTE_MAC),
            adapter("en5", DANTE_MAC, active=False),
        )
        found = self.find(Role.DANTE, Expected(DANTE_MAC), listing)
        self.assertEqual((found.status, found.name), (Status.PRESENT, "en4"))

    def test_untagged_control_picks_en7_and_never_its_vlan(self):
        found = self.find(Role.CONTROL, Expected(CONTROL_MAC))
        self.assertEqual((found.status, found.name, found.address), (Status.PRESENT, "en7", "198.51.100.123"))

    def test_untagged_control_is_missing_when_only_the_vlan_has_the_mac(self):
        listing = without(self.adapters, "en7")
        self.assertEqual(self.find(Role.CONTROL, Expected(CONTROL_MAC), listing).status, Status.MISSING)

    def test_a_bridge_is_never_the_adapter_even_when_it_is_active_and_the_real_one_is_not(self):
        # bridge0 shares en1's MAC and is "active"; en1 has no link.
        bridge = by_name(self.adapters, "bridge0")
        en1 = by_name(self.adapters, "en1")
        self.assertEqual(bridge.mac, en1.mac)
        listing = (en1, replace(bridge, active=True))
        found = self.find(Role.DANTE, Expected(en1.mac), listing)
        self.assertEqual((found.status, found.name), (Status.LINK_DOWN, "en1"))
        only_bridge = (listing[1],)
        self.assertEqual(self.find(Role.DANTE, Expected(en1.mac), only_bridge).status, Status.MISSING)

    def test_tagged_scoring_picks_the_vlan_interface(self):
        found = self.find(Role.SCORING, Expected(CONTROL_MAC, SCORING_TAG))
        self.assertEqual((found.status, found.name, found.address), (Status.PRESENT, "vlan0", "203.0.113.50"))
        self.assertEqual(found.vlan, SCORING_TAG)

    def test_a_tag_the_vlan_does_not_carry_is_no_vlan(self):
        # vlan0 carries 20; the config says 30, and en7 is there.
        listing = without(self.adapters, "vlan1")
        found = self.find(Role.SCORING, Expected(CONTROL_MAC, 30), listing)
        self.assertEqual((found.status, found.name), (Status.NO_VLAN, "en7"))

    def test_only_the_parent_present_is_no_vlan_naming_the_parent(self):
        listing = without(self.adapters, "vlan0")
        found = self.find(Role.SCORING, Expected(CONTROL_MAC, SCORING_TAG), listing)
        self.assertEqual((found.status, found.name), (Status.NO_VLAN, "en7"))

    def test_nothing_with_the_mac_is_missing_even_when_tagged(self):
        listing = without(self.adapters, "en7", "vlan0")
        found = self.find(Role.SCORING, Expected(CONTROL_MAC, SCORING_TAG), listing)
        self.assertEqual((found.status, found.name), (Status.MISSING, None))

    def test_a_parent_whose_cable_is_out_takes_its_vlan_down_with_it(self):
        out = interfaces.parse_ifconfig(MAC_CABLE_OUT)
        self.assertTrue(by_name(out, "vlan0").link_up)
        self.assertFalse(by_name(out, "en7").link_up)
        self.assertEqual(self.find(Role.SCORING, Expected(CONTROL_MAC, SCORING_TAG), out).status, Status.LINK_DOWN)
        self.assertEqual(self.find(Role.CONTROL, Expected(CONTROL_MAC), out).status, Status.LINK_DOWN)

    def test_a_vlan_with_no_parent_has_no_link(self):
        vlan1 = by_name(self.adapters, "vlan1")
        listing = (adapter("vlanX", vlan1.mac, vlan=30, parent=None),)
        found = self.find(Role.SCORING, Expected(vlan1.mac, 30), listing)
        self.assertEqual(found.status, Status.LINK_DOWN)

    def test_a_vlan_whose_parent_is_not_in_the_listing_has_no_link(self):
        listing = (adapter("vlan0", CONTROL_MAC, vlan=20, parent="en7"),)
        self.assertEqual(self.find(Role.SCORING, Expected(CONTROL_MAC, 20), listing).status, Status.LINK_DOWN)

    def test_a_vlan_with_only_a_link_local_address_has_no_address(self):
        listing = (
            adapter("en7", CONTROL_MAC),
            adapter("vlan0", CONTROL_MAC, vlan=20, parent="en7", ipv4=("169.254.1.1",)),
        )
        self.assertEqual(self.find(Role.SCORING, Expected(CONTROL_MAC, 20), listing).status, Status.NO_ADDRESS)

    def test_linux_listing_finds_the_same_roles(self):
        linux = interfaces.parse_ip_json(LINUX_TEXT)
        self.assertEqual(self.find(Role.CONTROL, Expected(CONTROL_MAC), linux).name, "enp3s0")
        scoring = self.find(Role.SCORING, Expected(CONTROL_MAC, 20), linux)
        self.assertEqual((scoring.status, scoring.name), (Status.PRESENT, "enp3s0.20"))
        dante = self.find(Role.DANTE, Expected(DANTE_MAC), linux)
        self.assertEqual((dante.status, dante.name), (Status.LINK_DOWN, "enx00005e005302"))

    def test_dante_never_needs_an_address(self):
        self.assertNotIn(Role.DANTE, interfaces.NEEDS_IPV4)
        self.assertEqual(interfaces.NEEDS_IPV4, {Role.CONTROL, Role.SCORING})


class TestAssess(unittest.TestCase):
    def test_only_configured_roles_in_role_order(self):
        adapters = interfaces.parse_ifconfig(MAC_TEXT)
        found = interfaces.assess({Role.SCORING: EXPECTED[Role.SCORING], Role.DANTE: EXPECTED[Role.DANTE]}, adapters)
        self.assertEqual([f.role for f in found], [Role.DANTE, Role.SCORING])

    def test_no_roles_is_no_findings(self):
        self.assertEqual(interfaces.assess({}, interfaces.parse_ifconfig(MAC_TEXT)), ())


def reading(adapters, *, trigger=interfaces.Trigger.POLL, at=1.0, detail=None):
    return interfaces.Reading(trigger, at, adapters, detail=detail)


def could_not(detail="ifconfig failed"):
    return reading(None, detail=detail)


class TestTrackerEdges(unittest.TestCase):
    def setUp(self):
        self.all_present = interfaces.parse_ifconfig(MAC_TEXT)
        self.no_dante = without(self.all_present, "en8")
        self.tracker = interfaces.Tracker(expected={Role.DANTE: EXPECTED[Role.DANTE]})

    def feed(self, tracker, *readings):
        seen = []
        for each in readings:
            tracker, observed = tracker.observe(each)
            seen.append(observed)
        return tracker, seen

    def test_a_role_missing_at_startup_goes_bad(self):
        _, (observed,) = self.feed(self.tracker, reading(self.no_dante, trigger=interfaces.Trigger.STARTUP))
        self.assertEqual([f.role for f in observed.went_bad], [Role.DANTE])
        self.assertTrue(observed.changed)

    def test_present_then_missing_goes_bad_once(self):
        _, (first, second, third) = self.feed(
            self.tracker, reading(self.all_present), reading(self.no_dante), reading(self.no_dante)
        )
        self.assertEqual(first.went_bad, ())
        self.assertEqual([f.status for f in second.went_bad], [Status.MISSING])
        self.assertEqual(third.went_bad, ())
        self.assertFalse(third.changed)

    def test_link_down_then_missing_is_not_a_new_raise_but_is_a_change(self):
        down = tuple(adapter("en8", DANTE_MAC, active=False) if a.name == "en8" else a for a in self.all_present)
        _, (_, second, third) = self.feed(
            self.tracker, reading(self.all_present), reading(down), reading(self.no_dante)
        )
        self.assertEqual([f.status for f in second.went_bad], [Status.LINK_DOWN])
        self.assertEqual(third.went_bad, ())
        self.assertTrue(third.changed)

    def test_back_and_gone_again_raises_again(self):
        _, observed = self.feed(
            self.tracker,
            reading(self.all_present),
            reading(self.no_dante),
            reading(self.all_present),
            reading(self.no_dante),
        )
        self.assertEqual([len(o.went_bad) for o in observed], [0, 1, 0, 1])

    def test_a_read_that_could_not_check_neither_raises_nor_resets_the_edge(self):
        tracker, observed = self.feed(
            self.tracker, reading(self.all_present), could_not(), reading(self.no_dante), reading(self.no_dante)
        )
        self.assertEqual([len(o.went_bad) for o in observed], [0, 0, 1, 0])
        # And a bad role stays bad through a could-not-check, so it does not raise on the far side.
        tracker, observed = self.feed(tracker, could_not(), reading(self.no_dante))
        self.assertEqual([len(o.went_bad) for o in observed], [0, 0])
        self.assertTrue(tracker.bad(Role.DANTE))

    def test_could_not_check_at_startup_then_missing_goes_bad(self):
        _, observed = self.feed(self.tracker, could_not(), reading(self.no_dante))
        self.assertEqual([len(o.went_bad) for o in observed], [0, 1])

    def test_could_not_check_is_a_change_once_and_again_on_recovery(self):
        _, observed = self.feed(
            self.tracker,
            reading(self.all_present),
            could_not("x"),
            could_not("x"),
            could_not("y"),
            reading(self.all_present),
        )
        self.assertEqual([o.changed for o in observed], [True, True, False, True, True])

    def test_a_name_or_address_change_is_a_change_with_no_raise(self):
        moved = tuple(
            adapter("en9", DANTE_MAC, ipv4=("169.254.1.1",)) if a.name == "en8" else a for a in self.all_present
        )
        _, (_, second) = self.feed(self.tracker, reading(self.all_present), reading(moved))
        self.assertTrue(second.changed)
        self.assertEqual(second.went_bad, ())

    def test_two_roles_going_bad_together_come_back_in_role_order(self):
        tracker = interfaces.Tracker(expected=EXPECTED)
        gone = without(self.all_present, "en7", "vlan0")
        _, (_, second) = self.feed(tracker, reading(self.all_present), reading(gone))
        self.assertEqual([f.role for f in second.went_bad], [Role.CONTROL, Role.SCORING])

    def test_bad_is_false_for_a_role_never_read(self):
        self.assertFalse(self.tracker.bad(Role.DANTE))
        self.assertFalse(self.tracker.bad(Role.CONTROL))

    def test_shares_with_names_the_other_role_on_the_same_mac(self):
        tracker = interfaces.Tracker(expected=EXPECTED)
        self.assertEqual(tracker.shares_with(Role.CONTROL), Role.SCORING)
        self.assertEqual(tracker.shares_with(Role.SCORING), Role.CONTROL)
        self.assertIsNone(tracker.shares_with(Role.DANTE))

    def test_the_snapshot_says_where_it_is(self):
        empty = interfaces.Tracker(expected={})
        self.assertEqual(empty.as_snapshot()["status"], "not-configured")
        self.assertEqual(self.tracker.as_snapshot()["status"], "not-checked")
        tracker, _ = self.tracker.observe(reading(self.all_present, at=7.0))
        snapshot = tracker.as_snapshot()
        self.assertEqual((snapshot["status"], snapshot["checked_at"]), ("checked", 7.0))
        self.assertEqual(snapshot["roles"][0]["name"], "en8")
        self.assertEqual(snapshot["roles"][0]["ipv4"], "169.254.33.7")
        failed, _ = tracker.observe(could_not("boom"))
        self.assertEqual(failed.as_snapshot()["status"], "could-not-check")
        self.assertEqual(failed.as_snapshot()["detail"], "boom")
        self.assertEqual(failed.as_snapshot()["roles"], snapshot["roles"])

    def test_the_log_data_is_flat_and_json_safe(self):
        tracker, _ = self.tracker.observe(reading(self.all_present, trigger=interfaces.Trigger.STARTUP))
        data = tracker.as_data()
        self.assertEqual(data["trigger"], "startup")
        self.assertEqual(json.loads(json.dumps(data)), data)


class TestFaultMessage(unittest.TestCase):
    def found(self, role, status, **kwargs):
        mac = kwargs.pop("mac", CONTROL_MAC)
        return Found(role, mac, kwargs.pop("vlan", None), status, kwargs.pop("name", "en7"), ())

    def test_every_status_and_role_names_the_role_the_mac_and_gives_its_advice(self):
        for role in Role:
            for status in (Status.MISSING, Status.LINK_DOWN, Status.NO_ADDRESS, Status.NO_VLAN):
                vlan = SCORING_TAG if status is Status.NO_VLAN else None
                text = interfaces.fault_message(self.found(role, status, vlan=vlan), shares_with=None)
                self.assertIn(interfaces.ROLE_LABELS[role], text)
                self.assertIn(CONTROL_MAC, text)
                self.assertIn(interfaces.ADVICE[role], text)
                text.encode("ascii")
                self.assertNotIn("{", text)

    def test_a_present_role_has_nothing_to_say(self):
        with self.assertRaises(ValueError):
            interfaces.fault_message(self.found(Role.DANTE, Status.PRESENT), shares_with=None)

    def test_link_down_and_no_address_name_the_interface(self):
        for status in (Status.LINK_DOWN, Status.NO_ADDRESS):
            self.assertIn("en7", interfaces.fault_message(self.found(Role.CONTROL, status), shares_with=None))

    def test_no_vlan_names_the_tag_and_the_parent(self):
        text = interfaces.fault_message(self.found(Role.SCORING, Status.NO_VLAN, vlan=SCORING_TAG), shares_with=None)
        self.assertIn(f"VLAN {SCORING_TAG} on en7", text)

    def test_the_shared_cable_sentence_names_the_other_role_only_when_one_is_given(self):
        alone = interfaces.fault_message(self.found(Role.CONTROL, Status.MISSING), shares_with=None)
        shared = interfaces.fault_message(self.found(Role.CONTROL, Status.MISSING), shares_with=Role.SCORING)
        self.assertNotIn("same adapter", alone)
        self.assertTrue(shared.endswith(interfaces.SHARED_CABLE.format(other="scoreboard (RTD)")))

    def test_only_a_physical_failure_gets_the_shared_cable_sentence_even_when_shared(self):
        for status in (Status.MISSING, Status.LINK_DOWN):
            text = interfaces.fault_message(self.found(Role.CONTROL, status), shares_with=Role.SCORING)
            self.assertIn("Same adapter and cable as the scoreboard (RTD) role", text, status)
        for status, vlan in ((Status.NO_VLAN, SCORING_TAG), (Status.NO_ADDRESS, None)):
            text = interfaces.fault_message(self.found(Role.SCORING, status, vlan=vlan), shares_with=Role.CONTROL)
            self.assertNotIn("Same adapter", text, status)

    def test_the_dante_advice_says_what_to_do_with_reaper(self):
        self.assertIn("Settings > Audio > Device", interfaces.ADVICE[Role.DANTE])


class TestConstants(unittest.TestCase):
    def test_a_read_always_finishes_before_the_next_is_due(self):
        self.assertGreater(interfaces.POLL_SECONDS, interfaces.READ_TIMEOUT_SECONDS)
        self.assertEqual(interfaces.READ_TIMEOUT_SECONDS, reach.ARP_TIMEOUT_SECONDS)

    def test_the_fault_kinds_cover_every_role_and_are_distinct(self):
        self.assertEqual(set(interfaces.FAULT_KINDS), set(Role))
        self.assertEqual(len(set(interfaces.FAULT_KINDS.values())), len(Role))
        interface_kinds = {kind for kind in faults.FaultKind if kind.value.startswith("interface-")}
        self.assertEqual(set(interfaces.FAULT_KINDS.values()), interface_kinds)

    def test_the_spec_numbers(self):
        self.assertEqual(interfaces.VLAN_MIN, 1)
        self.assertEqual(interfaces.VLAN_MAX, 4094)
        self.assertEqual(interfaces.POLL_SECONDS, 5.0)
        self.assertEqual(interfaces.IFCONFIG_ARGS, ("-a",))
        self.assertEqual(interfaces.IP_ARGS, ("-d", "-j", "addr", "show"))

    def test_the_words_are_ascii(self):
        for text in (
            interfaces.FAULT_MISSING,
            interfaces.FAULT_LINK_DOWN,
            interfaces.FAULT_NO_ADDRESS,
            interfaces.FAULT_NO_VLAN,
            interfaces.SHARED_CABLE,
            interfaces.NO_IPV4_DANTE,
            *interfaces.ADVICE.values(),
            *interfaces.STATUS_WORDS.values(),
        ):
            text.encode("ascii")


class TestExpectedFromConfig(unittest.TestCase):
    def test_a_string_is_an_untagged_adapter(self):
        self.assertEqual(interfaces.expected_from_config("00:00:5E:00:53:2"), Expected("00:00:5e:00:53:02", None))

    def test_a_table_carries_a_tag(self):
        self.assertEqual(interfaces.expected_from_config({"mac": CONTROL_MAC, "vlan": 20}), Expected(CONTROL_MAC, 20))
        self.assertEqual(interfaces.expected_from_config({"mac": CONTROL_MAC}), Expected(CONTROL_MAC, None))

    def test_refusals(self):
        for bad, words in (
            (True, "MAC address string or a table"),
            (5, "MAC address string or a table"),
            ("zz", "MAC address like"),
            ({"vlan": 20}, "mac"),
            ({"mac": CONTROL_MAC, "tag": 20}, "mac, vlan"),
            ({"mac": 5}, "MAC address like"),
            ({"mac": CONTROL_MAC, "vlan": 0}, "1 to 4094"),
            ({"mac": CONTROL_MAC, "vlan": 4095}, "1 to 4094"),
            ({"mac": CONTROL_MAC, "vlan": True}, "1 to 4094"),
            ({"mac": CONTROL_MAC, "vlan": "20"}, "1 to 4094"),
        ):
            with self.assertRaises(interfaces.ExpectedError, msg=repr(bad)) as caught:
                interfaces.expected_from_config(bad)
            self.assertIn(words, str(caught.exception))

    def test_the_range_edges_are_accepted(self):
        for tag in (interfaces.VLAN_MIN, interfaces.VLAN_MAX):
            self.assertEqual(interfaces.expected_from_config({"mac": CONTROL_MAC, "vlan": tag}).vlan, tag)


class TestFindTools(unittest.TestCase):
    def test_macos_wants_ifconfig_and_falls_back_to_the_system_dirs(self):
        calls = []

        def which(name, path=None):
            calls.append((name, path))
            return "/sbin/ifconfig" if path == "/sbin:/usr/sbin:/bin:/usr/bin" else None

        tools = interfaces.find_tools(reach.Platform.MACOS, which=which, path="/nowhere")
        self.assertEqual((tools.ifconfig, tools.ip), ("/sbin/ifconfig", None))
        self.assertEqual(calls[0], ("ifconfig", "/nowhere"))

    def test_linux_wants_ip(self):
        tools = interfaces.find_tools(reach.Platform.LINUX, which=lambda name, path=None: f"/bin/{name}", path="")
        self.assertEqual((tools.ifconfig, tools.ip), (None, "/bin/ip"))

    def test_an_unknown_platform_has_no_tools(self):
        self.assertEqual(interfaces.find_tools(None), interfaces.Tools(None, None, None))

    def test_a_missing_binary_is_none(self):
        tools = interfaces.find_tools(reach.Platform.MACOS, which=lambda name, path=None: None, path="")
        self.assertIsNone(tools.ifconfig)


class TestCommand(unittest.TestCase):
    def test_macos(self):
        self.assertEqual(interfaces.command(MAC_TOOLS), ["/sbin/ifconfig", "-a"])

    def test_linux(self):
        self.assertEqual(interfaces.command(LINUX_TOOLS), ["/sbin/ip", "-d", "-j", "addr", "show"])

    def test_no_tool_or_platform_is_no_command(self):
        self.assertIsNone(interfaces.command(interfaces.Tools(reach.Platform.MACOS, None, None)))
        self.assertIsNone(interfaces.command(interfaces.Tools(None, "/sbin/ifconfig", None)))


class FakeRunner:
    def __init__(self, result=None):
        self.result = result if result is not None else reach.ProcessOutput(0, MAC_TEXT.encode(), b"")
        self.calls: list[tuple[list[str], float]] = []
        self.last_spawn_ms = 1.5

    async def run(self, argv: Sequence[str], *, timeout: float) -> reach.ProcessOutput:
        self.calls.append((list(argv), timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def run(coroutine):
    return asyncio.run(coroutine)


def one_read(runner, tools=MAC_TOOLS):
    return run(interfaces.read(tools, trigger=interfaces.Trigger.POLL, runner=runner, monotonic=lambda: 3.0))


class TestRead(unittest.TestCase):
    def test_a_good_read_carries_the_adapters_and_the_timing(self):
        runner = FakeRunner()
        result = one_read(runner)
        assert result.adapters is not None
        self.assertEqual(by_name(result.adapters, "en8").mac, DANTE_MAC)
        self.assertEqual((result.trigger, result.at, result.detail), (interfaces.Trigger.POLL, 3.0, None))
        self.assertEqual(result.spawn_ms, 1.5)
        self.assertEqual(result.elapsed_ms, 0.0)

    def test_the_command_and_the_timeout_are_the_pinned_ones(self):
        runner = FakeRunner()
        one_read(runner)
        self.assertEqual(runner.calls, [(["/sbin/ifconfig", "-a"], interfaces.READ_TIMEOUT_SECONDS)])

    def test_linux_is_read_as_json(self):
        runner = FakeRunner(reach.ProcessOutput(0, LINUX_TEXT.encode(), b""))
        result = one_read(runner, LINUX_TOOLS)
        assert result.adapters is not None
        self.assertEqual(by_name(result.adapters, "enp3s0.20").vlan, 20)

    def could_not(self, result, words):
        self.assertIsNone(result.adapters)
        assert result.detail is not None
        self.assertIn(words, result.detail)

    def test_a_non_zero_exit_could_not_check_with_the_stderr(self):
        runner = FakeRunner(reach.ProcessOutput(1, b"", b"permission denied\n"))
        self.could_not(one_read(runner), "ifconfig failed (exit 1): permission denied")

    def test_an_unavailable_child_could_not_check_with_its_detail(self):
        runner = FakeRunner(reach.CheckUnavailableError("ifconfig did not finish within 2s and was stopped"))
        self.could_not(one_read(runner), "did not finish")

    def test_a_missing_tool_could_not_check_before_any_child_is_started(self):
        runner = FakeRunner()
        result = one_read(runner, interfaces.Tools(reach.Platform.MACOS, None, None))
        self.could_not(result, "ifconfig is not installed")
        self.assertEqual(runner.calls, [])

    def test_an_unsupported_platform_says_so(self):
        runner = FakeRunner()
        self.could_not(one_read(runner, interfaces.Tools(None, None, None)), "no adapter command known")
        self.assertEqual(runner.calls, [])

    def test_any_exception_is_could_not_check(self):
        self.could_not(one_read(FakeRunner(RuntimeError("boom"))), "RuntimeError: boom")

    def test_unreadable_output_is_could_not_check(self):
        self.could_not(one_read(FakeRunner(reach.ProcessOutput(0, b"hello\n", b""))), "could not be read")

    def test_zero_adapters_is_could_not_check_never_all_present(self):
        headers_only = (
            b"lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n"
        )
        self.could_not(one_read(FakeRunner(reach.ProcessOutput(0, headers_only, b""))), "no adapters")
        self.could_not(one_read(FakeRunner(reach.ProcessOutput(0, b"[]", b"")), LINUX_TOOLS), "no adapters")

    def test_a_cancellation_is_passed_on(self):
        runner = FakeRunner(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            one_read(runner)

    def test_startup_reading_is_tagged_startup_and_never_raises(self):
        result = interfaces.startup_reading(MAC_TOOLS, runner=FakeRunner(), monotonic=lambda: 2.0)
        self.assertEqual(result.trigger, interfaces.Trigger.STARTUP)
        self.assertIsNotNone(result.adapters)
        broken = interfaces.startup_reading(MAC_TOOLS, runner=FakeRunner(RuntimeError("x")), monotonic=lambda: 2.0)
        self.assertIsNone(broken.adapters)

    def test_startup_reading_survives_a_loop_that_cannot_start(self):
        with mock.patch("tacet.interfaces.asyncio.run", side_effect=RuntimeError("no loop")):
            result = interfaces.startup_reading(MAC_TOOLS, runner=FakeRunner(), monotonic=lambda: 2.0)
        assert result.detail is not None
        self.assertIn("no loop", result.detail)
        self.assertIsNone(result.adapters)


class TestTheRealRunnerIsReachs(unittest.TestCase):
    def test_the_shell_uses_reachs_runner_not_its_own(self):
        source = Path(interfaces.__file__).read_text(encoding="utf-8")
        self.assertIn("reach.AsyncioRunner", source)
        self.assertNotIn("create_subprocess_exec", source)


class WatchCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.runner = FakeRunner()
        self.results: list[interfaces.Reading] = []
        self.moving = False

    def watch(self, runner=None, on_result=None):
        return interfaces.Watch(
            MAC_TOOLS,
            runner=runner or self.runner,
            busy=lambda: self.moving,
            on_result=on_result or self.results.append,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )

    async def started(self, watch):
        task = asyncio.ensure_future(watch.run())
        self.addAsyncCleanup(self.stop, task)
        await self.clock.settle()
        return task

    async def stop(self, task):
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class TestTheWatch(WatchCase):
    async def test_it_polls_every_interval_and_not_at_once(self):
        await self.started(self.watch())
        self.assertEqual(self.runner.calls, [])
        await self.clock.advance(interfaces.POLL_SECONDS - 1)
        self.assertEqual(len(self.runner.calls), 0)
        await self.clock.advance(1)
        self.assertEqual(len(self.runner.calls), 1)
        await self.clock.advance(interfaces.POLL_SECONDS)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual({r.trigger for r in self.results}, {interfaces.Trigger.POLL})

    async def test_a_poll_due_during_a_fader_move_is_put_off_and_retried_soon(self):
        self.moving = True
        await self.started(self.watch())
        await self.clock.advance(interfaces.POLL_SECONDS)
        self.assertEqual(self.runner.calls, [])
        self.moving = False
        await self.clock.advance(reach.BUSY_RETRY_SECONDS)
        self.assertEqual(len(self.runner.calls), 1)

    async def test_a_raising_callback_does_not_kill_it(self):
        seen = []

        def callback(result):
            seen.append(result)
            raise RuntimeError("page is gone")

        task = await self.started(self.watch(on_result=callback))
        await self.clock.advance(interfaces.POLL_SECONDS * 2)
        self.assertEqual(len(seen), 2)
        self.assertFalse(task.done())

    async def test_a_read_that_raises_is_could_not_check_and_the_watch_goes_on(self):
        task = await self.started(self.watch(runner=FakeRunner(RuntimeError("boom"))))
        await self.clock.advance(interfaces.POLL_SECONDS * 2)
        self.assertEqual(len(self.results), 2)
        self.assertTrue(all(r.adapters is None for r in self.results))
        self.assertFalse(task.done())

    async def test_a_failing_busy_check_is_could_not_check_and_the_watch_goes_on(self):
        def busy():
            raise RuntimeError("no fader")

        watch = interfaces.Watch(
            MAC_TOOLS,
            runner=self.runner,
            busy=busy,
            on_result=self.results.append,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )
        task = await self.started(watch)
        await self.clock.advance(interfaces.POLL_SECONDS * 2)
        self.assertEqual(len(self.results), 2)
        self.assertIn("no fader", self.results[0].detail or "")
        self.assertFalse(task.done())


if __name__ == "__main__":
    unittest.main()
