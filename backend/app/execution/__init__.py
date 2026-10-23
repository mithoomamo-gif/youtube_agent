"""Execution layer (Round 2): job runner, DAG, scheduler, events, service.

Modules are built by parallel workers and imported lazily by
:mod:`app.execution.service`, so this package init stays empty on
purpose — importing the package must never fail just because one
sibling module has not landed yet.
"""
