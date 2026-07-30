# Security Policy

Report path traversal, archive extraction, command injection, credential disclosure, unsafe provider URL handling, malicious raster/mesh parsing, and denial-of-service findings privately to the repository maintainer.

TopoForge never passes user text through a shell for slicer execution, writes builds through staging directories, rejects non-empty destinations, checks 3MF archive paths/encryption/external relationships, keeps network credentials out of provenance, and applies raster cell budgets. Security fixes receive regression tests and a release note before disclosure.
