"""citycost — city cost-of-living intelligence from free public sources.

Three sources, each doing only what it is actually good at:

  discover  nomads.com MCP   filter the world down to candidates
  verify    numbeo.com HTML  itemised real prices for one city
  rank      numbeo.com HTML  558 cities x 6 indices, 7 verticals, 31 snapshots

Numbeo's Data API costs $260/month with no free tier. The same figures are
served as public HTML, and that is what this reads.
"""

__version__ = "1.1.1"
__all__ = ["__version__"]
