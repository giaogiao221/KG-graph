# book_engine v2 scaffold

This directory is the clean generic extraction framework.

Current status:
- Directory structure created.
- Core intermediate schemas created.
- CLI smoke-test created.
- Configuration skeleton created.
- Legacy v24.5 mainline remains unchanged.

Recommended implementation order:
1. document/heading_rebuilder.py
2. document/block_segmenter.py
3. tables/grid_rebuilder.py
4. tables/header_tree_builder.py
5. tables/topology_classifier.py
6. tables/axis_role_resolver.py
7. tables/condition_extractor.py
8. tables/condition_scope_resolver.py
9. tables/value_role_classifier.py
10. tables/semantic_planner.py
11. tables/record_compiler.py
12. export/schema59_exporter.py