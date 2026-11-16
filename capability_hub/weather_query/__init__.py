"""MET Norway near-term forecasts with governed, tool-free Pi explanations.

Explicit composition: HubCatalog((bundle_descriptor(),)). The operator supplies
the published agent_turn model policy; this package selects no provider/model.
Open-Meteo geocoding selects the first result, optionally country-pinned; no unique
city match or station observation is claimed. Retain both providers' attribution.
Set WEATHER_QUERY_USER_AGENT to an application identifier with genuine operator
contact information before ongoing MET Norway use. The development-only default
contains no invented contact. Review both providers' terms before deployment.
"""
