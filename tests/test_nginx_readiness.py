"""Exercise the real HTTPS readiness block without Nginx, network, or sleep."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/configure-nginx-remote.sh"


class NginxReadinessTests(unittest.TestCase):
    def run_check(self, scenario):
        source = SCRIPT.read_text()
        block = source.split("# Reload completion does not guarantee", 1)[1]
        block = "# Reload completion does not guarantee" + block.split(
            "sudo install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy", 1
        )[0]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            # Shell functions isolate the actual readiness code from external services.
            mocks = r'''
curl() {
  count=$(cat "$upload/count" 2>/dev/null || echo 0)
  count=$((count + 1)); echo "$count" >"$upload/count"
  printf '%s\n' "$*" >>"$upload/curl-arguments"
  if [[ $scenario == persistent || ( $scenario == transient && $count -lt 3 ) ]]; then
    echo 'curl: (60) SSL: certificate subject name does not match target hostname' >&2
    return 60
  fi
  if [[ $scenario == invalid ]]; then printf 'not JSON'; else printf '{"ok":true}'; fi
}
sleep() { echo "$*" >>"$upload/sleeps"; }
sudo() {
  [[ $* == 'nginx -T' ]] || return 1
  printf '%s\n' 'server { listen 127.0.0.1:443 ssl; server_name other.example; proxy_set_header Authorization private-secret; }'
}
'''
            environment = os.environ.copy()
            environment.update(upload=str(directory), hostname="prayer.example",
                               python=os.sys.executable, scenario=scenario)
            result = subprocess.run(["bash", "-c", "set -euo pipefail\numask 077\n" + mocks + block],
                                    env=environment, text=True, capture_output=True)
            attempts = int((directory / "count").read_text())
            arguments = (directory / "curl-arguments").read_text().splitlines()
            sleeps = (directory / "sleeps").read_text().splitlines()
            mode = (directory / "https-health.err").stat().st_mode & 0o777
        return result, attempts, arguments, sleeps, mode

    def test_transient_certificate_mismatch_recovers(self):
        result, attempts, arguments, sleeps, mode = self.run_check("transient")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeps, ["1", "1"])
        self.assertEqual(mode, 0o600)
        for argument in arguments:
            self.assertIn("--max-time 3", argument)
            self.assertIn("--resolve prayer.example:443:127.0.0.1", argument)
            self.assertNotIn("--insecure", argument)
            self.assertNotIn("-k", argument.split())

    def test_persistent_certificate_mismatch_reports_original_error(self):
        result, attempts, _, sleeps, _ = self.run_check("persistent")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(attempts, 15)
        self.assertEqual(len(sleeps), 14)
        self.assertIn("certificate subject name does not match", result.stderr)
        self.assertIn("listen 127.0.0.1:443 ssl;", result.stderr)
        self.assertIn("server_name other.example;", result.stderr)
        self.assertNotIn("private-secret", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("not valid JSON", result.stderr)

    def test_invalid_json_reports_health_error_without_traceback(self):
        result, attempts, _, _, _ = self.run_check("invalid")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(attempts, 15)
        self.assertIn("HTTPS health response is not valid JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
