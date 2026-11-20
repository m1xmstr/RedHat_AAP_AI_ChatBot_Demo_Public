The demo builder reads public PDFs in a separate process using the unmodified pypdf 7.1.1 pure-Python wheel. The BSD-3-Clause license is included inside the wheel. The builder verifies the SHA-256 digest before loading it. This allows the stock AAP execution environment to extract public PDF text without network package installation. Python 3.10 or later is required for this extractor.

Upstream distribution and hashes are available on [PyPI](https://pypi.org/project/pypdf/7.1.1/#files).
