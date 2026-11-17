// pm2 process list for Windows. See AUTOMATIC_STARTUP.txt (PM2 section).
const path = require("path");
const python = path.join(__dirname, ".venv", "Scripts", "python.exe");

const controller = (mode, port, data) => ({
  name: `luminext-${mode}`,
  cwd: __dirname,
  script: python,
  interpreter: "none",
  args: ["-m", "luminext", "--mode", mode, "--host", "127.0.0.1", "--port", String(port), "--data", data],
  env: { PYTHONUNBUFFERED: "1" },
  autorestart: true,
  restart_delay: 5000,
  windowsHide: true,
});

module.exports = {
  apps: [
    controller("edge", 8000, path.join(__dirname, "data")),
    controller("cloud", 8001, path.join(__dirname, "data", "cloud")),
  ],
};
