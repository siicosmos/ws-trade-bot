"""Shared foundation for the ws-trade-bot apps.

core/ holds what both the info server and the consumer app
execute: config loading, the SQLite store, the alert parser,
and the common operations modules (supervision, updater,
watchdog, log webhooks, notifications). Role-specific code
lives in info/ and consumer/.
"""
