# Security policy

Please report suspected vulnerabilities through a private GitHub security advisory rather than a
public issue. Include a synthetic reproduction and impact assessment; never include credentials or
private data.

Security fixes are supported for the latest released minor version. Because Parity executes user
supplied Python, process isolation is a reliability boundary, not a hostile-code sandbox. This is
intentional and documented in `docs/SECURITY.md` and `docs/THREAT_MODEL.md`.
