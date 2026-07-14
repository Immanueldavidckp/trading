#!/usr/bin/env python3
import json
import os
from pathlib import Path

# Create output directory
os.makedirs('graphify-out', exist_ok=True)

# Combined extraction from both chunks
extraction = {
  "nodes": [
    {"id": "main_app", "label": "FastAPI Application", "file_type": "code", "source_file": "backend/main.py"},
    {"id": "upstox_client", "label": "UpstoxClient", "file_type": "code", "source_file": "backend/upstox_client.py"},
    {"id": "upstox_feed", "label": "UpstoxQuoteFeed", "file_type": "code", "source_file": "backend/upstox_feed.py"},
    {"id": "analysis", "label": "analysis module", "file_type": "code", "source_file": "backend/analysis.py"},
    {"id": "smc_engine", "label": "smc_engine", "file_type": "code", "source_file": "backend/smc_engine.py"},
    {"id": "fvg_trade_plan", "label": "fvg_trade_plan", "file_type": "code", "source_file": "backend/fvg_trade_plan.py"},
    {"id": "plan_engine", "label": "plan_engine", "file_type": "code", "source_file": "backend/plan_engine.py"},
    {"id": "plan_pipeline", "label": "plan_pipeline", "file_type": "code", "source_file": "backend/plan_pipeline.py"},
    {"id": "plan_score", "label": "plan_score", "file_type": "code", "source_file": "backend/plan_score.py"},
    {"id": "db", "label": "db module", "file_type": "code", "source_file": "backend/db.py"},
    {"id": "universe", "label": "universe", "file_type": "code", "source_file": "backend/universe.py"},
    {"id": "ai_agent", "label": "AIAgent", "file_type": "code", "source_file": "backend/ai_agent.py"},
    {"id": "upstox_autologin", "label": "upstox_autologin", "file_type": "code", "source_file": "backend/upstox_autologin.py"},
    {"id": "App_jsx", "label": "App React Component", "file_type": "code", "source_file": "frontend/src/App.jsx"},
    {"id": "main_jsx", "label": "React Entry Point", "file_type": "code", "source_file": "frontend/src/main.jsx"},
    {"id": "vite_config", "label": "Vite Configuration", "file_type": "config", "source_file": "frontend/vite.config.js"},
    {"id": "deploy_workflow", "label": "Deploy Workflow", "file_type": "config", "source_file": ".github/workflows/deploy.yml"},
    {"id": "project_log", "label": "Project Log", "file_type": "document", "source_file": "PROJECT_LOG.txt"},
    {"id": "root_readme", "label": "Project README", "file_type": "document", "source_file": "README.md"},
    {"id": "index_html", "label": "Dashboard Page", "file_type": "code", "source_file": "backend/static/index.html"},
    {"id": "live_html", "label": "Live Analysis Page", "file_type": "code", "source_file": "backend/static/live.html"},
    {"id": "plan_html", "label": "Plan Report Page", "file_type": "code", "source_file": "backend/static/plan.html"},
    {"id": "echarts_lib", "label": "ECharts Library", "file_type": "dependency"},
    {"id": "react_lib", "label": "React 19", "file_type": "dependency"},
    {"id": "pm2", "label": "PM2 Process Manager", "file_type": "deployment"},
    {"id": "lightsail", "label": "AWS Lightsail", "file_type": "deployment"},
  ],
  "edges": [
    {"source": "main_app", "target": "upstox_client", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "main_app", "target": "upstox_feed", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "main_app", "target": "analysis", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "main_app", "target": "smc_engine", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "main_app", "target": "plan_pipeline", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "main_app", "target": "db", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "analysis", "target": "smc_engine", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "analysis", "target": "fvg_trade_plan", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "fvg_trade_plan", "target": "smc_engine", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "plan_pipeline", "target": "plan_engine", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "plan_pipeline", "target": "plan_score", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "plan_pipeline", "target": "db", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "plan_pipeline", "target": "universe", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "plan_engine", "target": "smc_engine", "relation": "calls", "confidence": "EXTRACTED"},
    {"source": "plan_score", "target": "plan_engine", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "upstox_client", "target": "db", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "main_jsx", "target": "App_jsx", "relation": "calls", "confidence": "EXTRACTED"},
    {"source": "App_jsx", "target": "react_lib", "relation": "imports", "confidence": "EXTRACTED"},
    {"source": "App_jsx", "target": "main_app", "relation": "references", "confidence": "EXTRACTED"},
    {"source": "vite_config", "target": "react_lib", "relation": "references", "confidence": "EXTRACTED"},
    {"source": "index_html", "target": "main_app", "relation": "calls", "confidence": "INFERRED"},
    {"source": "live_html", "target": "main_app", "relation": "calls", "confidence": "INFERRED"},
    {"source": "live_html", "target": "echarts_lib", "relation": "calls", "confidence": "EXTRACTED"},
    {"source": "plan_html", "target": "main_app", "relation": "calls", "confidence": "INFERRED"},
    {"source": "deploy_workflow", "target": "main_app", "relation": "deploys", "confidence": "EXTRACTED"},
    {"source": "deploy_workflow", "target": "pm2", "relation": "uses", "confidence": "EXTRACTED"},
    {"source": "deploy_workflow", "target": "lightsail", "relation": "targets", "confidence": "EXTRACTED"},
    {"source": "project_log", "target": "main_app", "relation": "documents", "confidence": "EXTRACTED"},
    {"source": "root_readme", "target": "main_app", "relation": "documents", "confidence": "EXTRACTED"},
  ]
}

# Save extraction
Path('.graphify_extract.json').write_text(json.dumps(extraction, indent=2))
print(f"Extraction saved: {len(extraction['nodes'])} nodes, {len(extraction['edges'])} edges")

# Now build graph using networkx
try:
  import networkx as nx

  G = nx.DiGraph()
  for node in extraction['nodes']:
    G.add_node(node['id'], **node)

  for edge in extraction['edges']:
    G.add_edge(edge['source'], edge['target'], **{k: v for k, v in edge.items() if k not in ['source', 'target']})

  print(f"Graph built: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

  # Simple clustering - just group by file_type for now
  communities = {}
  community_id = 0
  file_types = {}

  for node_id in G.nodes():
    node_data = G.nodes[node_id]
    ft = node_data.get('file_type', 'other')
    if ft not in file_types:
      file_types[ft] = community_id
      community_id += 1
    communities.setdefault(file_types[ft], []).append(node_id)

  communities = {i: v for i, v in enumerate(communities.values())}
  print(f"Communities: {len(communities)}")

  # Save graph.json in graphify format
  graph_data = {
    "nodes": [
      {"id": nid, "label": G.nodes[nid].get('label', nid), "type": G.nodes[nid].get('file_type', 'code')}
      for nid in G.nodes()
    ],
    "links": [
      {"source": u, "target": v, "relation": G[u][v].get('relation', 'references')}
      for u, v in G.edges()
    ],
    "communities": communities
  }

  Path('graphify-out/graph.json').write_text(json.dumps(graph_data, indent=2))
  print("✓ graphify-out/graph.json")

  # Generate basic report
  report = f"""# Knowledge Graph Report

## Overview
- **Total Nodes**: {G.number_of_nodes()}
- **Total Edges**: {G.number_of_edges()}
- **Communities**: {len(communities)}

## Architecture

### Backend Services
The FastAPI application (main_app) is the core, coordinating:
- **Data Acquisition**: UpstoxClient & UpstoxFeed for market data
- **Analysis Engine**: SMC analysis (smc_engine), FVG trading plans
- **Planning Pipeline**: Daily plan generation and scoring
- **Database**: SQLite storage for candles, price changes, plans

### Frontend
React application (App_jsx) communicates with FastAPI backend via:
- Dashboard pages (index.html, live.html, plan.html)
- ECharts for visualizations

### Deployment
- GitHub Actions workflow deploys to AWS Lightsail
- PM2 manages the trading-backend process

## Major Components

### Analysis Pipeline
1. **SMC Engine**: Detects swing points, market structure, fair value gaps
2. **FVG Trade Plans**: Generates trading plans based on FVG patterns
3. **Plan Engine**: Builds composite trading plans
4. **Plan Score**: Evaluates plan quality

### Data Layer
- **Candles Table**: OHLCV data from Upstox
- **Price Changes**: Market price updates
- **Daily Plans**: Generated trading plans
- **Plan Results**: Backtested plan performance
- **Plan Scorecard**: Aggregated scores

## Key Dependencies
- React 19 (frontend)
- Vite (frontend build)
- FastAPI (backend)
- ECharts (charting)

## Suggested Questions
1. How does market data flow from Upstox to the trading plans?
2. What's the relationship between SMC analysis and FVG trading?
3. How are plans scored and evaluated?
4. What's the deployment pipeline?
5. How does the frontend consume backend APIs?
"""

  Path('graphify-out/GRAPH_REPORT.md').write_text(report)
  print("✓ graphify-out/GRAPH_REPORT.md")

  print("\n✅ Graph build complete!")
  print(f"\nOutputs saved to graphify-out/")

except Exception as e:
  print(f"Error: {e}")
  import traceback
  traceback.print_exc()
