# Shotcut 26.6 compatibility fixtures

These bounded MLT documents use the Shotcut 26.6.25 and MLT 7.40 serialization
conventions exercised by the public project interface. Tests copy a fixture to a
temporary directory before editing it, then reload the serialized result to verify
timeline behavior and preservation of unowned properties.

Media references are intentionally unresolved. Unit tests replace external MLT
validation; the opt-in integration suite creates real temporary media and validates
the same operations with the installed Shotcut/MLT runtime.
