# Insurance Application Data Validation Agent

AI agent for validating insurance application data, built with Agentic Star.

> **Category**: Cat 2 (domain-specific pipeline)
> **Industry**: Insurance
> **Template ID**: INS-C2-006

## Overview

Checks a submitted insurance application against an underwriting rule set and returns a
field-level validation report: which fields passed, which need correcting, the risk flags the
submission raised, and an overall decision of `PASS`, `NIGO` (not in good order — correctable and
resubmittable) or `REJECT`.

The rules live in `config/rules.yaml` and are loaded at runtime: required fields, supported policy
types, code formats, and one or more named threshold profiles (coverage band, eligibility window,
term and beneficiary limits). A caller picks a profile per request, so the same agent can serve
several products without a code change.

Two properties are enforced rather than assumed. Every number in a submitted application is parsed
through a finite, bounded parser before it is compared against a threshold — `NaN` survives a plain
`float()` call and compares False against every bound, which would report an unbounded coverage
amount as acceptable. And the report is assembled from a closed vocabulary — field names, result
labels, risk flags and threshold-derived remediation sentences — which the output boundary
independently re-derives from the rule set and checks line by line, so no applicant value can reach
the caller.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent manifest, runtime parameters, underwriting rule set
docs/         design and test specification
```

See `docs/02_design.md` for the pipeline and the input/output contracts, and
`docs/03_test_spec.md` for the test cases behind them.

## Customising

1. Edit `config/rules.yaml` — required fields, supported policy types, and the threshold
   profiles. This is live configuration; the code holds no second copy of these values.
2. Adjust `config/config.yaml` for your own runtime parameters.
3. Review the node implementations under `src/nodes/` for domain-specific logic, and
   `src/schemas/report_contract.py` if you change what the report may contain.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
