"""Web slicer: a slicer in the dashboard, with OrcaSlicer running on worker computers (docs/slicer.md).

- meshes.py: STL files, read and checked as untrusted input, placed with each object's transform.
- profiles.py: OrcaSlicer's printer, process and filament presets, flattened for its command line.
- settings.py: the slicing settings people can change, checked before they reach a worker.
- jobs.py: the slicing queue and the workers (SQLite), with leases so a job lost with its worker runs again.
- web.py: the Slicer tab's API, the workers' API and the worker installer.
- worker.py: the worker service that runs on each slicing computer (standard library only).
"""
