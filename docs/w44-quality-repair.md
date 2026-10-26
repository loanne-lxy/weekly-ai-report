# W44 quality repair

The run clock is frozen once in main. Exa uses [cutoff - 7 days, cutoff]; article
preflight enforces the same interval, including RFC RSS timestamps. Unknown dates
are not treated as current and cannot enter Top Events. Original arXiv publication
and update time are separate. Short RSS bodies are retried with precision extraction;
unrecoverable content is quarantined with an audit reason.

Complete-link proposals require all pairwise original-space cosine similarities
>= 0.72 and at most eight articles. This is a conservative initial operating point,
not a universal semantic truth threshold. Concrete project/paper/release identity is
checked by the curator; every article must be assigned exactly once. Every retained
member requires a source-verifiable quote and event relevance >= 0.75. Different
papers cannot be merged back merely because their topics match. Model failures
quarantine content; singleton events remain valid.

Scoring: (0.85 * (0.4 Importance + 0.3 Novelty + 0.3 Impact) +
0.15 * (0.7 publisher reference quality + 0.3 recency)) * evidence relevance * decay.
Publisher scores are editorial heuristics, not measured probabilities. Article
quantity, source quantity and GitHub popularity do not increase confidence.
Scientific papers use a 14-day half-life. Missing baselines are disclosed.

All score rationales, evidence relevance, excerpts, quality state and final_score
are stored in events.metadata_json with queryable gate/score columns. A weekly
snapshot replacement removes obsolete event relationships transactionally. Older
weeks' event records are retained. Top Events filters quality before ranking.

Replay safely against an isolated database copy:

    venv/bin/python main.py --from-cache INPUT.json --replay --week 2026-W44 \
      --cutoff 2026-10-26T00:00:01Z --data-dir STAGING_DATA --output-dir STAGING_OUTPUT

Replay bypasses URL registry writes and source discovery. It writes raw input,
article dispositions and curated events under DATA/runs/WEEK/. It does not run
scripts/weekly_run.sh and therefore does not commit, push or deploy.

Tests:

    venv/bin/python tests/test_w44_quality.py
    venv/bin/python tests/test_w37_fix.py

Source excerpts establish traceability, not independent scientific replication.
The observations explicitly state evidence limitations and do not claim changes
against earlier weeks without historical comparison data.
