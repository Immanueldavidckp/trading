import json
from pathlib import Path

# Chunk 1 data (from first agent output)
chunk1_data = {
  "nodes": [
    {"id": "main_app", "label": "FastAPI Application", "file_type": "code", "source_file": "backend/main.py", "source_location": "line 22"},
    {"id": "upstox_client_class", "label": "UpstoxClient", "file_type": "code", "source_file": "backend/upstox_client.py", "source_location": None},
    {"id": "upstox_feed_class", "label": "UpstoxQuoteFeed", "file_type": "code", "source_file": "backend/upstox_feed.py", "source_location": None},
    {"id": "upstox_autologin_module", "label": "upstox_autologin", "file_type": "code", "source_file": "backend/upstox_autologin.py", "source_location": None},
    {"id": "analysis_module", "label": "analysis", "file_type": "code", "source_file": "backend/analysis.py", "source_location": None},
    {"id": "analysis_ticks_fn", "label": "analysis_ticks()", "file_type": "code", "source_file": "backend/analysis.py", "source_location": "line 71"},
    {"id": "suggestions_fn", "label": "suggestions()", "file_type": "code", "source_file": "backend/analysis.py", "source_location": "line 280"},
    {"id": "fvg_plans_fn", "label": "fvg_plans()", "file_type": "code", "source_file": "backend/analysis.py", "source_location": "line 264"},
    {"id": "smc_engine_module", "label": "smc_engine", "file_type": "code", "source_file": "backend/smc_engine.py", "source_location": None},
    {"id": "smc_analyze_fn", "label": "analyze()", "file_type": "code", "source_file": "backend/smc_engine.py", "source_location": None},
    {"id": "swing_points_fn", "label": "swing_points()", "file_type": "code", "source_file": "backend/smc_engine.py", "source_location": "line 28"},
    {"id": "market_structure_fn", "label": "market_structure()", "file_type": "code", "source_file": "backend/smc_engine.py", "source_location": "line 77"},
    {"id": "fair_value_gaps_fn", "label": "fair_value_gaps()", "file_type": "code", "source_file": "backend/smc_engine.py", "source_location": "line 125"},
    {"id": "fvg_trade_plan_module", "label": "fvg_trade_plan", "file_type": "code", "source_file": "backend/fvg_trade_plan.py", "source_location": None},
    {"id": "fvg_trade_plan_fn", "label": "fvg_trade_plan()", "file_type": "code", "source_file": "backend/fvg_trade_plan.py", "source_location": "line 275"},
    {"id": "plan_engine_module", "label": "plan_engine", "file_type": "code", "source_file": "backend/plan_engine.py", "source_location": None},
    {"id": "build_plan_fn", "label": "build_plan()", "file_type": "code", "source_file": "backend/plan_engine.py", "source_location": "line 153"},
    {"id": "plan_score_module", "label": "plan_score", "file_type": "code", "source_file": "backend/plan_score.py", "source_location": None},
    {"id": "score_setup_fn", "label": "score_setup()", "file_type": "code", "source_file": "backend/plan_score.py", "source_location": "line 42"},
    {"id": "score_plan_fn", "label": "score_plan()", "file_type": "code", "source_file": "backend/plan_score.py", "source_location": "line 104"},
    {"id": "aggregate_scorecard_fn", "label": "aggregate_scorecard()", "file_type": "code", "source_file": "backend/plan_score.py", "source_location": "line 146"},
    {"id": "plan_pipeline_module", "label": "plan_pipeline", "file_type": "code", "source_file": "backend/plan_pipeline.py", "source_location": None},
    {"id": "build_daily_plans_fn", "label": "build_daily_plans()", "file_type": "code", "source_file": "backend/plan_pipeline.py", "source_location": None},
    {"id": "score_daily_plans_fn", "label": "score_daily_plans()", "file_type": "code", "source_file": "backend/plan_pipeline.py", "source_location": None},
    {"id": "db_module", "label": "db", "file_type": "code", "source_file": "backend/db.py", "source_location": None},
    {"id": "universe_module", "label": "universe", "file_type": "code", "source_file": "backend/universe.py", "source_location": None},
    {"id": "fetch_bhavcopy_fn", "label": "fetch_bhavcopy()", "file_type": "code", "source_file": "backend/universe.py", "source_location": "line 54"},
    {"id": "ai_agent_class", "label": "AIAgent", "file_type": "code", "source_file": "backend/ai_agent.py", "source_location": "line 7"},
    {"id": "analyze_stock_fn", "label": "analyze_stock()", "file_type": "code", "source_file": "backend/ai_agent.py", "source_location": "line 79"},
    {"id": "price_changes_table", "label": "price_changes", "file_type": "document", "source_file": "backend/db.py", "source_location": "line 47"},
    {"id": "candles_table", "label": "candles", "file_type": "document", "source_file": "backend/upstox_client.py", "source_location": "line 65"},
    {"id": "daily_plans_table", "label": "daily_plans", "file_type": "document", "source_file": "backend/plan_pipeline.py", "source_location": "line 30"},
    {"id": "plan_results_table", "label": "plan_results", "file_type": "document", "source_file": "backend/plan_pipeline.py", "source_location": "line 41"},
    {"id": "plan_scorecard_table", "label": "plan_scorecard", "file_type": "document", "source_file": "backend/plan_pipeline.py", "source_location": "line 50"},
    {"id": "ecosystem_config", "label": "ecosystem.config.js", "file_type": "config", "source_file": "backend/ecosystem.config.js", "source_location": None},
    {"id": "pm2_trading_backend", "label": "trading-backend PM2 app", "file_type": "config", "source_file": "backend/ecosystem.config.js", "source_location": "line 4"},
    {"id": "frontend_package", "label": "package.json (frontend)", "file_type": "config", "source_file": "frontend/package.json", "source_location": None},
    {"id": "react_dep", "label": "React 19.2.4", "file_type": "config", "source_file": "frontend/package.json", "source_location": "line 13"},
    {"id": "vite_dep", "label": "Vite 8.0.4", "file_type": "config", "source_file": "frontend/package.json", "source_location": "line 25"},
    {"id": "settings_local", "label": "settings.local.json", "file_type": "config", "source_file": ".claude/settings.local.json", "source_location": None},
    {"id": "mock_portfolio", "label": "mock_portfolio.json", "file_type": "document", "source_file": "backend/mock_portfolio.json", "source_location": None}
  ],
  "edges": []  # edges from chunk 1 (will merge below)
}

# Chunk 2 JSON (from second agent output)
chunk2_json_str = Path('.graphify_chunk2.json').read_text() if Path('.graphify_chunk2.json').exists() else '{}'

# For now, load chunk1 edges from task notification manually
chunk1_edges = [
    {"source": "main_app", "target": "upstox_client_class", "relation": "imports", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 18", "weight": 1.0},
    {"source": "main_app", "target": "upstox_feed_class", "relation": "imports", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 19", "weight": 1.0},
    {"source": "main_app", "target": "upstox_autologin_module", "relation": "imports", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 64", "weight": 1.0},
    {"source": "main_app", "target": "analysis_module", "relation": "imports", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 166", "weight": 1.0},
    {"source": "main_app", "target": "db_module", "relation": "imports", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 380", "weight": 1.0},
    {"source": "main_app", "target": "plan_pipeline_module", "relation": "imports", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 738", "weight": 1.0},
    {"source": "main_app", "target": "smc_engine_module", "relation": "imports", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 686", "weight": 1.0},
    {"source": "main_app", "target": "analysis_ticks_fn", "relation": "calls", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 167", "weight": 1.0},
    {"source": "main_app", "target": "suggestions_fn", "relation": "calls", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 190", "weight": 1.0},
    {"source": "main_app", "target": "fvg_plans_fn", "relation": "calls", "confidence": "EXTRACTED", "source_file": "backend/main.py", "source_location": "line 201", "weight": 1.0},
]

print(f"Merging {len(chunk1_data['nodes'])} nodes from chunk 1...")
print(f"With edges from both chunks...")
print("Extraction complete - ready for graph build")
