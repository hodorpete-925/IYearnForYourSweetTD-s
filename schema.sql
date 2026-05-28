-- =========================================================================
-- DIMENSION TABLES
-- =========================================================================

CREATE TABLE seasons (
    season            INTEGER PRIMARY KEY,
    nfl_game_id       INTEGER NOT NULL,
    yahoo_league_id   TEXT    NOT NULL,
    league_key        TEXT    NOT NULL UNIQUE
);

CREATE TABLE managers (
    manager_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    yahoo_guid        TEXT    NOT NULL UNIQUE,
    nickname          TEXT    NOT NULL,
    full_name         TEXT
);

CREATE TABLE players (
    player_id         INTEGER PRIMARY KEY,
    player_name       TEXT    NOT NULL,
    position          TEXT,
    nfl_team          TEXT
);

CREATE TABLE teams (
    team_season_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    season            INTEGER NOT NULL,
    yahoo_team_id     INTEGER NOT NULL,
    team_name         TEXT    NOT NULL,
    manager_id        INTEGER NOT NULL,
    FOREIGN KEY (season)     REFERENCES seasons(season),
    FOREIGN KEY (manager_id) REFERENCES managers(manager_id),
    UNIQUE (season, yahoo_team_id)
);

CREATE TABLE drc_dollar_lookup (
    drc               INTEGER PRIMARY KEY,
    drc_dollars       INTEGER NOT NULL
);

-- =========================================================================
-- FACT TABLES
-- =========================================================================

CREATE TABLE transactions (
    transaction_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    yahoo_transaction_id      INTEGER  NOT NULL,
    season                    INTEGER  NOT NULL,
    timestamp                 DATETIME NOT NULL,
    event_type                TEXT     NOT NULL,
    status                    TEXT     NOT NULL,
    FOREIGN KEY (season) REFERENCES seasons(season),
    UNIQUE (season, yahoo_transaction_id),
    CHECK (event_type IN ('add', 'drop', 'add/drop', 'trade', 'commish'))
);

CREATE TABLE transaction_players (
    transaction_id              INTEGER NOT NULL,
    player_id                   INTEGER NOT NULL,
    direction                   TEXT    NOT NULL,
    team_season_id              INTEGER,
    source_type                 TEXT    NOT NULL,
    destination_type            TEXT    NOT NULL,
    counterparty_team_season_id INTEGER,
    PRIMARY KEY (transaction_id, player_id, direction),
    FOREIGN KEY (transaction_id)              REFERENCES transactions(transaction_id),
    FOREIGN KEY (player_id)                   REFERENCES players(player_id),
    FOREIGN KEY (team_season_id)              REFERENCES teams(team_season_id),
    FOREIGN KEY (counterparty_team_season_id) REFERENCES teams(team_season_id),
    CHECK (direction        IN ('incoming', 'outgoing')),
    CHECK (source_type      IN ('waivers', 'freeagents', 'team', 'draft')),
    CHECK (destination_type IN ('waivers', 'freeagents', 'team'))
);

CREATE TABLE transaction_picks (
    transaction_id              INTEGER NOT NULL,
    draft_round                 INTEGER NOT NULL,
    source_team_season_id       INTEGER NOT NULL,
    destination_team_season_id  INTEGER NOT NULL,
    original_team_season_id     INTEGER NOT NULL,
    PRIMARY KEY (transaction_id, draft_round, source_team_season_id),
    FOREIGN KEY (transaction_id)             REFERENCES transactions(transaction_id),
    FOREIGN KEY (source_team_season_id)      REFERENCES teams(team_season_id),
    FOREIGN KEY (destination_team_season_id) REFERENCES teams(team_season_id),
    FOREIGN KEY (original_team_season_id)    REFERENCES teams(team_season_id)
);

CREATE TABLE draft_picks (
    season            INTEGER NOT NULL,
    overall_pick      INTEGER NOT NULL,
    draft_round       INTEGER NOT NULL,
    pick_in_round     INTEGER NOT NULL,
    team_season_id    INTEGER NOT NULL,
    player_id         INTEGER NOT NULL,
    is_keeper         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season, overall_pick),
    FOREIGN KEY (season)         REFERENCES seasons(season),
    FOREIGN KEY (team_season_id) REFERENCES teams(team_season_id),
    FOREIGN KEY (player_id)      REFERENCES players(player_id)
);

-- =========================================================================
-- DERIVED TABLE — populated by Phase B
-- =========================================================================

CREATE TABLE roster_seasons (
    player_id            INTEGER NOT NULL,
    team_season_id       INTEGER NOT NULL,
    drc                  INTEGER NOT NULL,
    drc_dollars          INTEGER NOT NULL,
    acquisition_event    TEXT    NOT NULL,
    draft_round          INTEGER,
    pick_in_round        INTEGER,
    kept_from_previous   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, team_season_id),
    FOREIGN KEY (player_id)      REFERENCES players(player_id),
    FOREIGN KEY (team_season_id) REFERENCES teams(team_season_id),
    CHECK (drc >= 1 AND drc <= 16),
    CHECK (acquisition_event IN ('drafted', 'kept', 'trade_acquired', 'waiver_pickup', 'free_agent_pickup'))
);

-- =========================================================================
-- MANUAL OVERRIDES — Pete's commissioner annotations
-- =========================================================================

CREATE TABLE transaction_overrides (
    transaction_id           INTEGER PRIMARY KEY,
    override_type            TEXT    NOT NULL,        -- e.g. 'trade_from'
    source_team_season_id    INTEGER,                 -- for 'trade_from': the actual source team
    note                     TEXT,                    -- free-text explanation
    FOREIGN KEY (transaction_id)        REFERENCES transactions(transaction_id),
    FOREIGN KEY (source_team_season_id) REFERENCES teams(team_season_id),
    CHECK (override_type IN ('trade_from'))
);
-- =========================================================================
-- FINAL ROSTERS — end-of-season snapshots (raw data; DRC computed later)
-- =========================================================================

CREATE TABLE final_rosters (
    season              INTEGER NOT NULL,
    team_season_id      INTEGER NOT NULL,
    player_id           INTEGER NOT NULL,
    selected_position   TEXT,                       -- "QB", "WR", "BN", "IR", etc.
    is_keeper_yahoo     INTEGER NOT NULL DEFAULT 0, -- bool: Yahoo's is_keeper.kept
    keeper_cost_yahoo   INTEGER,                    -- Yahoo's reported cost (often NULL)
    PRIMARY KEY (season, team_season_id, player_id),
    FOREIGN KEY (season)         REFERENCES seasons(season),
    FOREIGN KEY (team_season_id) REFERENCES teams(team_season_id),
    FOREIGN KEY (player_id)      REFERENCES players(player_id)
);
-- =========================================================================
-- SEED DATA — DRC dollar lookup (static, will rarely change)
-- =========================================================================

INSERT INTO drc_dollar_lookup (drc, drc_dollars) VALUES
    (1, 200), (2, 100), (3, 80),  (4, 60),  (5, 50),
    (6, 30),  (7, 30),  (8, 30),  (9, 30),
    (10, 10), (11, 10), (12, 10), (13, 10), (14, 10), (15, 10), (16, 10);

-- =========================================================================
-- VIEWS — unioned real + synthetic transactions, with vetoed filtered out
-- =========================================================================
-- all_transactions and all_transaction_players are the entry points for the
-- DRC walk. They union real Yahoo-ingested transactions with synthetic ones
-- (used to model off-season trades Yahoo didn't capture cleanly), and
-- exclude any transaction whose status != 'successful' (i.e. vetoed,
-- rejected, or pending trade proposals). Synthetic trades are always
-- treated as successful since they're manually curated.

CREATE VIEW all_transactions AS
    SELECT transaction_id, timestamp, event_type, season, status, 0 AS is_synthetic
    FROM transactions
    WHERE status = 'successful'
    UNION ALL
    SELECT synth_id + 1000000 AS transaction_id, timestamp, event_type, season,
           'successful' AS status, 1 AS is_synthetic
    FROM synthetic_transactions;

CREATE VIEW all_transaction_players AS
    SELECT tp.transaction_id, tp.player_id, tp.direction, tp.team_season_id,
           tp.source_type, tp.destination_type, tp.counterparty_team_season_id,
           0 AS is_synthetic
    FROM transaction_players tp
    JOIN transactions t ON t.transaction_id = tp.transaction_id
    WHERE t.status = 'successful'
    UNION ALL
    SELECT synth_id + 1000000 AS transaction_id, player_id, direction, team_season_id,
           source_type, destination_type, counterparty_team_season_id,
           1 AS is_synthetic
    FROM synthetic_transaction_players;