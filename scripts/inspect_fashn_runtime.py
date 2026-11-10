"""Check the separate FASHN runtime without PowerShell inline-code quoting."""

import sys

import torch


print("Python:", sys.version.split()[0])
print("Torch:", torch.__version__, "CUDA:", torch.version.cuda)
print("CUDA available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("GPU:", torch.cuda.get_device_name(0))
raise SystemExit(0 if torch.cuda.is_available() else 1)
