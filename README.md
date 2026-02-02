# SU - Sandboxed Agent

SU is an autonomous agent that runs in EC2 and executes jobs on a schedule, from triggers, and other events. SU uses Claude Code to perform tasks while maintaining strict security through sandboxed execution to protect against prompt injection and other security risks.

## Overview

SU provides a secure, automated execution environment for AI-assisted tasks in the cloud. All agent actions are sandboxed to ensure safe operation even when processing untrusted inputs or responding to external triggers.

## Features

- **Scheduled Execution**: Run jobs on a regular schedule
- **Event-Driven Triggers**: Respond to external events and triggers
- **Claude Code Integration**: Leverages Claude Code for intelligent task execution
- **Sandboxed Security**: All actions are executed in isolated environments to prevent prompt injection attacks
- **Cloud-Native**: Runs on EC2 with full AWS integration

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                          AWS VPC                             │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              EC2 Instance (SU Agent)               │ │
│  │                                                     │ │
│  │  ┌─────────────────────────────────────────────┐  │ │
│  │  │      SU Agent Runtime                       │  │ │
│  │  │                                             │  │ │
│  │  │  • Claude Code CLI                          │  │ │
│  │  │  • Job Scheduler                            │  │ │
│  │  │  • Event Listeners                          │  │ │
│  │  │  • Sandboxed Execution Environment          │  │ │
│  │  │                                             │  │ │
│  │  └─────────────────────────────────────────────┘  │ │
│  │                                                     │ │
│  └────────────────────────────────────────────────────┘ │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                          │
                          │
                          ▼
                ┌──────────────────┐
                │ Trigger Sources  │
                │                  │
                │ • Schedules      │
                │ • Webhooks       │
                │ • Events         │
                └──────────────────┘
```

## Prerequisites

1. **AWS Account** with appropriate permissions
2. **Terraform** installed (>= 1.0)
3. **AWS CLI** configured with credentials
4. **VPC with internet gateway** for instance connectivity

## Quick Start

### 1. Clone and Configure

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with your values:

```hcl
aws_region = "us-east-1"
vpc_id     = "vpc-xxxxx"
subnet_id  = "subnet-xxxxx"
key_name   = "your-key-pair"
```

### 2. Deploy Infrastructure

```bash
# Initialize Terraform
terraform init

# Review the plan
terraform plan

# Deploy
terraform apply
```

### 3. Access the Instance

Use AWS Systems Manager for secure access:

```bash
aws ssm start-session --target $(terraform output -raw instance_id) --region us-east-1
```

## Security

SU implements multiple layers of security:

- **Sandboxed Execution**: All Claude Code actions run in isolated environments
- **Prompt Injection Protection**: Sandboxing prevents malicious inputs from compromising the system
- **Network Isolation**: Runs in VPC with controlled network access
- **IAM Roles**: Minimal permissions following the principle of least privilege

## Cleanup

To destroy all resources:

```bash
terraform destroy
```

Confirm by typing `yes` when prompted.

## License

This infrastructure code is provided as-is for personal or commercial use.