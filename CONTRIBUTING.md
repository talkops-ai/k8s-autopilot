# Contributing to k8s-autopilot

First off, thank you for considering contributing to **k8s-autopilot**! It's people like you that make open source such a fantastic community to learn, inspire, and create.

This document provides guidelines and best practices for contributing to the repository.

## Table of Contents
- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [How to Contribute](#how-to-contribute)
  - [Reporting Bugs](#reporting-bugs)
  - [Suggesting Enhancements](#suggesting-enhancements)
  - [Pull Request Process](#pull-request-process)
- [Local Development Setup](#local-development-setup)
- [Questions & Support](#questions--support)

---

## Code of Conduct
By participating in this project, you are expected to uphold a welcoming, respectful, and collaborative environment. Please be kind and constructive in your feedback and interactions.

## Getting Started
Before you begin, please ensure you have a basic understanding of Kubernetes, Docker, and Python, as these form the core of the **k8s-autopilot** architecture.

### Finding Work
If you are looking for ways to contribute, check our issue tracker. Issues labeled `good first issue` or `help wanted` are great places to start.

## How to Contribute

### Reporting Bugs
If you find a bug, please create an issue and include:
- A clear, descriptive title.
- Steps to reproduce the behavior.
- Expected behavior vs. actual behavior.
- Your environment details (OS, Docker version, Python version, Kubernetes cluster type).
- Any relevant logs (e.g., from the `k8s-autopilot` or `talkops-ui` containers).

### Suggesting Enhancements
We welcome ideas for new MCP servers, sub-agents, and general improvements! When proposing an enhancement, please include:
- The problem your enhancement solves.
- A proposed solution or feature description.
- Examples of how it would be used.

### Pull Request Process
1. **Fork the repo** and create your branch from `main`.
2. **Write clear, descriptive commit messages**.
3. **Test your changes** locally to ensure they don't break existing functionality.
4. **Update documentation** (like `README.md` or `.env.example`) if your changes introduce new configuration options or modify existing flows.
5. **Open a Pull Request** describing what you changed and why. Mention any related issue numbers (e.g., `Fixes #123`).

## Local Development Setup

To run the project from source and test your changes:

1. **Install Prerequisites:**
   - Docker & Docker Compose
   - [uv](https://docs.astral.sh/uv/getting-started/installation/) (for Python dependency management)
   - Python 3.12+

2. **Clone and Install:**
   ```bash
   git clone https://github.com/talkopsai/k8s-autopilot.git
   cd k8s-autopilot
   uv sync
   source .venv/bin/activate
   ```

3. **Configure Environment:**
   Copy `.env.example` to `.env` and configure your API keys (e.g., `GOOGLE_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`).

4. **Run the Development Environment:**
   You can spin up the required backing services (ArgoCD, Prometheus, etc.) and run the python agent locally.
   ```bash
   docker compose -f docker-compose-dev.yml up -d
   # Run the agent from source
   python -m agent.server
   ```

## Questions & Support
If you have any questions or need help setting up your development environment, feel free to open a "Question" issue or reach out in our community channels.

Thank you for contributing!
