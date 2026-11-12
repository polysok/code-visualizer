"""Natural-language search over the code graph.

* :mod:`.settings`  - embedding / LLM configuration (CViz config file).
* :mod:`.embedders` - local (fastembed) and OpenAI-compatible embedders.
* :mod:`.graph_index` - search columns, BM25 keyword ranking and the LadybugDB ``vector`` index.
* :mod:`.indexer`   - writes keyword texts and vectors on the artifact nodes
  (index, watch, ``cviz embed``, the page's Vectorize button).
* :mod:`.search`    - hybrid semantic + keyword search, ranked by the indexes.
* :mod:`.chat`      - LLM re-ranking and optional answer.

Everything lives in the graph database: the artifacts carry their keyword
text and vector, and LadybugDB's indexes rank them.
"""
