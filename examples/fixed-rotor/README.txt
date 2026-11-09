Host-only fixed rotor

After `python -m pip install .`:
  sim validate examples/fixed-rotor/scene.yaml
  sim run examples/fixed-rotor/scene.yaml

This exercises scene admission, lifecycle and idle C620 feedback generation.
It uses neither firmware nor a CPU model, QEMU, ARM tools or MuJoCo. It does
not establish a firmware feedback loop. Use examples/can-motor for that gate.
