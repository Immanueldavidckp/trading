// pm2 process definition for the trading backend.
//
// The app's packages live in backend/.venv (the box's system python is
// externally managed and has none of them). Starting plain `python3` under pm2
// therefore dies on the first import and pm2 marks the app errored — which is
// exactly how the site went down once. Prefer the venv whenever it exists.
const fs = require('fs');
const path = require('path');

const backend = __dirname;
const venvPython = path.join(backend, '.venv', 'bin', 'python');
const python = fs.existsSync(venvPython) ? venvPython : 'python3';

module.exports = {
  apps: [
    {
      name: 'trading-backend',
      script: python,
      interpreter: 'none',
      args: '-m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1',
      cwd: backend,
      autorestart: true,
      watch: false,
      // Restart backoff: a boot crash retried flat out made pm2 give up and
      // left the site down. Wait between attempts, and stop after a run.
      min_uptime: '20s',
      max_restarts: 10,
      restart_delay: 5000,
      exp_backoff_restart_delay: 2000,
      max_memory_restart: '600M',
      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
};
