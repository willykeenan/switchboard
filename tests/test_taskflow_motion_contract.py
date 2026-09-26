#!/usr/bin/env python3
"""Source contract for courier travel. Reads taskflow.js only; never opens board.sqlite3."""

import re
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / 'taskflow.js').read_text()
CSS = (Path(__file__).resolve().parents[1] / 'taskflow.css').read_text()


class TaskFlowMotionContract(unittest.TestCase):
    def test_outbound_phases_are_pickup_moving_handoff(self):
        compact = re.sub(r'\s+', '', SRC)
        self.assertIn("phase=motion.elapsed<600?'pickup':motion.elapsed<5400?'moving':'handoff'", compact)
        self.assertRegex(SRC, r"\(snapshot\.birds\s*\|\|\s*\[\]\)\.find\(b=>b\.taskId===t\.taskId")
        self.assertRegex(SRC, r"(?:icon\.)?dataset\.phase\s*=\s*phase")

    def test_drop_off_returns_bird_to_birdhouse(self):
        self.assertIn("leg:'home'", SRC)
        self.assertIn('tf-flight-bird', SRC)
        self.assertIn('tf-birdhouse', SRC)
        self.assertIn('dataset.perch', SRC)
        self.assertIn('.tf-flight-bird[data-phase=home] .tf-paper', CSS)
        self.assertIn('.tf-roost-bird[data-perch=empty]', CSS)

    def test_6200_is_not_the_only_lifetime(self):
        self.assertRegex(SRC, r"elapsed\s*>=\s*6200")
        self.assertRegex(SRC, r"elapsed\s*>=\s*2800")
        other = [n for n in re.findall(r"elapsed\s*[<>=]{1,3}\s*(\d+)", SRC) if n != '6200']
        self.assertTrue(other, '6200 must not be the only motion lifetime')

    def test_accept_mid_flight_keeps_outbound_until_dropoff(self):
        compact = re.sub(r'\s+', '', SRC)
        self.assertIn("liveCarry=!!carrier||t.state==='OFFERED'||String(station||'').startsWith('transit:')", compact)
        self.assertIn('if(courierId&&position&&liveCarry)', compact)
        self.assertIn("inFlight=!!motion||liveCarry", compact)

    def test_home_leg_landing_releases_before_away(self):
        compact = re.sub(r'\s+', '', SRC)
        self.assertIn(
            'if(flight.elapsed>=2800){releaseFlight(id);continue;}constsprite=flightSprite(id)',
            compact,
        )

    def test_replay_does_not_occupy_outbound_or_start_home_flight(self):
        compact = re.sub(r'\s+', '', SRC)
        self.assertIn('if(courierId&&position&&liveCarry)', compact)
        self.assertNotIn('if(courierId&&position&&inFlight)', compact)
        self.assertIn('motion.replay', compact)


if __name__ == '__main__':
    unittest.main()
