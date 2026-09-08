"""VM-unit readiness: dependent services start only after guest SSH is available."""
import subprocess
import time
from .service import ssh_argv

def main():
    deadline=time.monotonic()+100
    while time.monotonic()<deadline:
        try:
            result=subprocess.run(ssh_argv("true"),stdin=subprocess.DEVNULL,capture_output=True,timeout=6)
            if result.returncode==0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(1)
    raise SystemExit("guest SSH did not become ready; dependent services were not started")

if __name__=="__main__":
    main()
