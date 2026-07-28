# Agentic T-Shirt System

Automated pipeline for generating, judging, and publishing t-shirt designs using LLMs.

## Overview

This system orchestrates an end-to-end workflow:
1. **Ingest** - Pull content ideas via Xpoz MCP
2. **Generate** - Create t-shirt design briefs using NVIDIA NIM LLMs
3. **Judge** - Score and filter designs (anti-slop, overall quality)
4. **Approve** - Automated approval based on thresholds
5. **Publish** - Send approved designs to Printify
6. **Traffic** - Configure SEO, email, community, and paid social promotion

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env  # Fill in your API keys
python main.py
```

## Configuration

- `config.yaml` - Pipeline traffic and judging parameters
- `.env.example` - Environment variable template

## Project Structure

```
├── main.py              # Entry point
├── config.yaml          # Pipeline configuration
├── requirements.txt     # Python dependencies
├── src/
│   ├── orchestrator.py  # Pipeline coordination
│   ├── generator.py     # LLM design generation
│   ├── judge.py         # Scoring & filtering
│   ├── approval.py      # Approval workflow
│   ├── traffic.py       # Traffic configuration
│   ├── publisher.py     # Printify integration
│   ├── ingest.py        # Content ingestion (Xpoz MCP)
│   ├── llm_client.py    # NVIDIA NIM client
│   ├── llm_utils.py     # LLM utilities
│   ├── rate_governor.py # Rate limiting
│   ├── database.py      # SQLite schema
│   ├── brief_schema.py  # Design brief schema
│   ├── analysis.py      # Analysis utilities
│   └── logger.py        # Logging setup
```

## Requirements

- Python 3.14+
- NVIDIA NIM API key
- Telegram bot token (for notifications)
- Xpoz MCP API key
- Printify API key
