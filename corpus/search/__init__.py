"""Full-text and semantic search over the reviewed corpus.

Nothing is re-exported here on purpose. `search.rebuild` reached through
the package and `search.rebuild` reached through the module would be two
names for one function, and patching or reassigning either would leave
the other untouched. Import the module: `from corpus.search import search`.
"""
