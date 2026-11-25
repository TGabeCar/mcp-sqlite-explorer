"""Build the synthetic "mission control" demo database.

Every spacecraft, call sign, mission and event below is invented. The data is
generated with a fixed random seed, so running the script twice gives the same
rows.

    python examples/make_demo_db.py                     # writes examples/mission_control.db
    python examples/make_demo_db.py path/to/other.db    # writes somewhere else
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("mission_control.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE spacecraft (
    spacecraft_id   INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL UNIQUE,
    class           TEXT    NOT NULL CHECK (class IN ('shuttle', 'lander', 'probe', 'station')),
    crew_capacity   INTEGER NOT NULL DEFAULT 0,
    commissioned_on DATE    NOT NULL
);

CREATE TABLE crew_members (
    crew_id     INTEGER PRIMARY KEY,
    call_sign   TEXT    NOT NULL UNIQUE,
    role        TEXT    NOT NULL,
    home_base   TEXT    NOT NULL,
    joined_on   DATE    NOT NULL
);

CREATE TABLE missions (
    mission_id     INTEGER PRIMARY KEY,
    code           TEXT    NOT NULL UNIQUE,
    spacecraft_id  INTEGER NOT NULL REFERENCES spacecraft(spacecraft_id) ON DELETE RESTRICT,
    destination    TEXT    NOT NULL,
    status         TEXT    NOT NULL CHECK (status IN ('planned', 'active', 'complete', 'scrubbed')),
    launch_date    DATE,
    duration_days  INTEGER,
    budget_mcr     REAL    -- budget in millions of (fictional) credits
);
CREATE INDEX idx_missions_status ON missions(status);
CREATE INDEX idx_missions_spacecraft ON missions(spacecraft_id);

CREATE TABLE mission_crew (
    mission_id  INTEGER NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    crew_id     INTEGER NOT NULL REFERENCES crew_members(crew_id) ON DELETE CASCADE,
    seat        TEXT    NOT NULL,
    PRIMARY KEY (mission_id, crew_id)
);

CREATE TABLE telemetry_events (
    event_id     INTEGER PRIMARY KEY,
    mission_id   INTEGER NOT NULL REFERENCES missions(mission_id) ON DELETE CASCADE,
    logged_at    TIMESTAMP NOT NULL,
    severity     TEXT    NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    subsystem    TEXT    NOT NULL,
    message      TEXT    NOT NULL
);
CREATE INDEX idx_events_mission_time ON telemetry_events(mission_id, logged_at);

CREATE VIEW mission_summary AS
SELECT m.code,
       s.name            AS spacecraft,
       m.destination,
       m.status,
       COUNT(DISTINCT mc.crew_id) AS crew,
       COUNT(DISTINCT e.event_id) AS events
FROM missions m
JOIN spacecraft s        ON s.spacecraft_id = m.spacecraft_id
LEFT JOIN mission_crew mc ON mc.mission_id = m.mission_id
LEFT JOIN telemetry_events e ON e.mission_id = m.mission_id
GROUP BY m.mission_id;
"""

SPACECRAFT = [
    ("Halcyon Drift", "shuttle", 6, "2031-03-14"),
    ("Pale Lantern", "lander", 3, "2032-07-01"),
    ("Kestrel IX", "probe", 0, "2030-11-22"),
    ("Tessellate", "station", 12, "2029-05-09"),
    ("Orrery Wren", "shuttle", 5, "2033-01-30"),
    ("Basalt Moth", "lander", 2, "2034-09-17"),
]

CALL_SIGNS = [
    "Nebula", "Ferrite", "Quasar", "Lumen", "Halo", "Cinder", "Vector", "Zenith",
    "Pulsar", "Drift", "Ember", "Orbit", "Sable", "Tundra", "Comet", "Nimbus",
    "Aster", "Flux", "Glacier", "Rook",
]
ROLES = ["commander", "pilot", "flight engineer", "mission specialist", "medic", "geologist"]
BASES = ["Outpost Meridian", "Harbor Station Nine", "Crater Lake Field", "Ridgeline Annex"]

DESTINATIONS = [
    "Lunar south pole", "Mars orbit", "Phobos", "Ceres", "Europa flyby",
    "L2 relay point", "Vesta", "Titan survey", "Low Earth orbit",
]
GREEK = ["Alpha", "Bravo", "Cosmo", "Delta", "Echo", "Foxtrot", "Gamma", "Helix", "Ion", "Juno"]
SUBSYSTEMS = ["propulsion", "life support", "comms", "navigation", "power", "thermal", "payload"]
MESSAGES = {
    "info": [
        "Routine status check nominal",
        "Course correction burn complete",
        "Downlink window opened",
        "Crew sleep cycle started",
        "Solar array tracking the sun",
    ],
    "warning": [
        "Coolant loop pressure trending high",
        "Star tracker lost lock, reacquiring",
        "Battery state of charge below 40%",
        "Packet loss above threshold",
    ],
    "critical": [
        "Main engine shutdown earlier than planned",
        "Cabin CO2 scrubber fault, switched to backup",
        "Loss of signal for 12 minutes",
    ],
}


def build(path: Path) -> Path:
    rng = random.Random(42)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)

        conn.executemany(
            "INSERT INTO spacecraft (name, class, crew_capacity, commissioned_on) VALUES (?, ?, ?, ?)",
            SPACECRAFT,
        )

        crew = []
        for call_sign in CALL_SIGNS:
            joined = date(2026, 1, 1) + timedelta(days=rng.randint(0, 2500))
            crew.append((call_sign, rng.choice(ROLES), rng.choice(BASES), joined.isoformat()))
        conn.executemany(
            "INSERT INTO crew_members (call_sign, role, home_base, joined_on) VALUES (?, ?, ?, ?)",
            crew,
        )

        missions = []
        for i in range(24):
            craft_id = rng.randint(1, len(SPACECRAFT))
            status = rng.choices(["complete", "active", "planned", "scrubbed"], weights=[10, 4, 5, 2])[0]
            launch = date(2034, 1, 1) + timedelta(days=rng.randint(0, 1400))
            duration = None if status in ("planned", "scrubbed") else rng.randint(3, 420)
            code = f"{GREEK[i % len(GREEK)]}-{100 + i}"
            budget = round(rng.uniform(40, 2400), 1)
            missions.append(
                (code, craft_id, rng.choice(DESTINATIONS), status,
                 None if status == "scrubbed" else launch.isoformat(), duration, budget)
            )
        conn.executemany(
            "INSERT INTO missions (code, spacecraft_id, destination, status, launch_date, duration_days, budget_mcr) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            missions,
        )

        seats = ["commander", "pilot", "specialist 1", "specialist 2", "specialist 3", "specialist 4"]
        crew_rows = []
        for mission_id, (_, craft_id, *_rest) in enumerate(missions, start=1):
            capacity = SPACECRAFT[craft_id - 1][2]
            if capacity == 0:
                continue
            size = rng.randint(1, min(capacity, len(seats)))
            for seat, crew_id in zip(seats, rng.sample(range(1, len(CALL_SIGNS) + 1), size)):
                crew_rows.append((mission_id, crew_id, seat))
        conn.executemany("INSERT INTO mission_crew (mission_id, crew_id, seat) VALUES (?, ?, ?)", crew_rows)

        events = []
        for mission_id, (_, _, _, status, launch, _, _) in enumerate(missions, start=1):
            if status not in ("active", "complete") or launch is None:
                continue
            t = datetime.fromisoformat(launch) + timedelta(hours=6)
            for _ in range(rng.randint(4, 14)):
                t += timedelta(minutes=rng.randint(20, 3000))
                severity = rng.choices(["info", "warning", "critical"], weights=[14, 5, 1])[0]
                events.append(
                    (mission_id, t.isoformat(sep=" "), severity, rng.choice(SUBSYSTEMS),
                     rng.choice(MESSAGES[severity]))
                )
        conn.executemany(
            "INSERT INTO telemetry_events (mission_id, logged_at, severity, subsystem, message) VALUES (?, ?, ?, ?, ?)",
            events,
        )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return path


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    out = build(target)
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")
