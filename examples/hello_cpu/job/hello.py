"""The job itself: runs on the VM, not on your laptop. It just says hi."""

import os
import platform

print("hi from gcejob")
print(f"job id : {os.environ.get('GCEJOB_JOB_ID', '(not running under gcejob)')}")
print(f"host   : {platform.node()}")
print(f"python : {platform.python_version()}")
