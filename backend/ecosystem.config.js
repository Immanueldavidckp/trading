module.exports = {
  apps: [
    {
      name: 'trading-backend',
      script: 'python3',
      args: '-m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1',
      cwd: '/home/ubuntu/trading/backend',
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
};
