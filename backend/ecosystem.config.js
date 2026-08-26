// pm2 definition for the trading backend.
//
// Started/reloaded by deploy/deploy.sh:
//   pm2 startOrReload ecosystem.config.js --update-env
const fs = require('fs');
const path = require('path');

// Prefer the venv the deploy installs requirements into, so the interpreter
// pm2 launches is always the one that has the deps. Falls back to the system
// python for a hand-started server with no venv yet.
const venvPython = path.join(__dirname, '.venv', 'bin', 'python');
const python = fs.existsSync(venvPython) ? venvPython : '/usr/bin/python3';

module.exports = {
  apps: [
    {
      name: 'trading-backend',
      script: python,
      args: ['-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', '8000', '--workers', '1'],
      interpreter: 'none', // exec the python binary directly, don't wrap it
      cwd: __dirname,
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',

      // A crash on boot used to be retried flat-out — that's the 89 restarts
      // in `pm2 status`. Back off instead, and give up after 10 fast failures
      // so a broken release fails its health check loudly (and gets rolled
      // back) rather than hammering the box. The counter resets once the
      // process stays up for min_uptime.
      min_uptime: '30s',
      max_restarts: 10,
      exp_backoff_restart_delay: 2000,
      kill_timeout: 10000,

      out_file: path.join(__dirname, 'logs', 'backend-out.log'),
      error_file: path.join(__dirname, 'logs', 'backend-error.log'),
      time: true,

      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
};
