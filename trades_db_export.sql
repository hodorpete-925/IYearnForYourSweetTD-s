-- Export every trade event (real + synthetic) for 2023-2024 league years
-- to compare against Pete's "All Transactions (2)" Excel sheet.
--
-- Each row = one player moving from one manager to another in a single trade event.
-- Direction is normalized so each player appears once per trade (FROM -> TO).
--
-- Run in DBeaver, then right-click result -> Export Data -> CSV
-- Save as: trades_db_export.csv  in the project folder.

SELECT
    DATE(t.timestamp)                          AS trade_date,
    t.timestamp                                AS trade_ts,
    t.transaction_id                           AS txn_id,
    CASE WHEN t.is_synthetic = 1 THEN 'synthetic' ELSE 'real' END AS source_table,
    p.player_name                              AS player,
    mc.full_name                               AS from_manager,
    md.full_name                               AS to_manager,
    t.event_type                               AS event_type,
    t.season                                   AS season
FROM all_transactions t
JOIN all_transaction_players tp
       ON tp.transaction_id = t.transaction_id
JOIN players p
       ON p.player_id = tp.player_id
LEFT JOIN teams td
       ON td.team_season_id = tp.team_season_id
LEFT JOIN managers md
       ON md.manager_id = td.manager_id
LEFT JOIN teams tc
       ON tc.team_season_id = tp.counterparty_team_season_id
LEFT JOIN managers mc
       ON mc.manager_id = tc.manager_id
WHERE t.event_type = 'trade'
  AND tp.direction = 'incoming'   -- only direction stored; counterparty = FROM, team = TO
  AND DATE(t.timestamp) <  '2025-09-01'   -- match Excel coverage window
ORDER BY t.timestamp, p.player_name;
