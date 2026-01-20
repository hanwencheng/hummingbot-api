---
name: deploy-test
description: Deploy a trading strategy bot, check logs, and debug issues. Use when deploying bots, testing strategies, or troubleshooting.
allowed-tools:
  - Read
  - Bash
---

# Deploy and Test Trading Strategy

Deploy a bot, monitor logs, analyze issues, and hand over to user for review.

## Script Location

```
/Users/hanwencheng/Projects/hummingbot-api/.claude/skills/deploy-test/deploy-bot.sh
```

## Workflow

### 1. Deploy Bot

```bash
/Users/hanwencheng/Projects/hummingbot-api/.claude/skills/deploy-test/deploy-bot.sh {bot_name} deploy {config_name}
```

### 2. Check Status

```bash
/Users/hanwencheng/Projects/hummingbot-api/.claude/skills/deploy-test/deploy-bot.sh {bot_name} status
```

### 3. Get Logs

```bash
/Users/hanwencheng/Projects/hummingbot-api/.claude/skills/deploy-test/deploy-bot.sh {bot_name} logs
```

Logs are saved to `/tmp/{bot_name}-logs.txt` for analysis.

### 4. Get Errors Only

```bash
/Users/hanwencheng/Projects/hummingbot-api/.claude/skills/deploy-test/deploy-bot.sh {bot_name} errors
```

### 5. Analyze Logs

Read the saved log file:

```bash
cat /tmp/{bot_name}-logs.txt
```

Look for these patterns:

| Pattern | Issue | Solution |
|---------|-------|----------|
| `Connection refused` | Exchange API down | Check exchange status |
| `Invalid credentials` | Wrong API keys | Re-add credentials |
| `Insufficient balance` | No funds | Deposit funds |
| `Order rejected` | Bad price/size | Check trading rules |
| `MQTT connection failed` | Broker down | `docker restart emqx` |

### 6. Hand Over to User

After analysis, present findings to user:
- Summary of errors found
- Recommended fixes
- Ask if user wants to stop the bot and fix manually

### 7. Stop Bot (if requested)

```bash
/Users/hanwencheng/Projects/hummingbot-api/.claude/skills/deploy-test/deploy-bot.sh {bot_name} stop
```

## Other Commands

List all running bots:

```bash
/Users/hanwencheng/Projects/hummingbot-api/.claude/skills/deploy-test/deploy-bot.sh any list
```

Follow live logs:

```bash
/Users/hanwencheng/Projects/hummingbot-api/.claude/skills/deploy-test/deploy-bot.sh {bot_name} follow
```
