# Changelog

## 1.2.1

Repository extracted from the supplied Smart Home Teacher installer, preserving its embedded Home Assistant integration files and runtime behavior.

Packaging changes in this repository:
- Split the integration into standard `custom_components/smart_home_teacher` files.
- Added HACS repository metadata and GitHub static validation.
- Added installation, architecture, development and safety documentation.
- Did not include the original host-level installer because it removes prior project files, edits HA storage/configuration and restarts containers.
