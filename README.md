# Ubuntu Desktop with Tailscale - Terraform Infrastructure

This Terraform configuration deploys a secure Ubuntu 22.04 Desktop EC2 instance with NICE DCV for remote desktop access via Tailscale.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                          AWS VPC                             │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │              EC2 Instance (t3.medium)              │    │
│  │                                                     │    │
│  │  ┌─────────────────────────────────────────────┐  │    │
│  │  │      Ubuntu 22.04 Desktop (Minimal)         │  │    │
│  │  │                                             │  │    │
│  │  │  • NICE DCV Server (port 8443)              │  │    │
│  │  │  • Tailscale VPN Client                     │  │    │
│  │  │  • Google Chrome                            │  │    │
│  │  │  • Syncthing (browser profile sync)         │  │    │
│  │  │                                             │  │    │
│  │  └─────────────────────────────────────────────┘  │    │
│  │                                                     │    │
│  │  Root Volume: 50GB gp3 (encrypted)                 │    │
│  │  IAM Role: SSM Managed Instance Core               │    │
│  │                                                     │    │
│  └────────────────────────────────────────────────────┘    │
│                          │                                   │
│                          │                                   │
│  ┌───────────────────────▼──────────────────────────────┐  │
│  │           Security Group                             │  │
│  │  • Ingress: SSH (22) - Optional, configurable       │  │
│  │  • Egress: All traffic allowed                      │  │
│  │  • No public DCV ports (Tailscale access only)      │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                          │
                          │
            ┌─────────────┴─────────────┐
            │                           │
            ▼                           ▼
    ┌──────────────┐          ┌──────────────────┐
    │ AWS Systems  │          │   Tailscale VPN  │
    │   Manager    │          │     Network      │
    │   (SSM)      │          │                  │
    └──────────────┘          └──────────────────┘
            │                           │
            └─────────────┬─────────────┘
                          │
                          ▼
                  ┌───────────────┐
                  │  Your Device  │
                  │               │
                  │  • SSM CLI    │
                  │  • DCV Viewer │
                  │  • Tailscale  │
                  └───────────────┘
```

## Features

- **Secure Access**: No public desktop ports; remote desktop access via Tailscale only
- **Systems Manager**: SSM-enabled for secure shell access without SSH keys
- **Ubuntu Desktop**: Minimal Ubuntu 22.04 desktop environment
- **NICE DCV**: High-performance remote desktop protocol with web viewer
- **Browser Tools**: Pre-installed Google Chrome
- **File Sync**: Syncthing for browser profile synchronization
- **Encrypted Storage**: 50GB gp3 root volume with encryption enabled

## Prerequisites

1. **AWS Account** with appropriate permissions
2. **Terraform** installed (>= 1.0)
3. **AWS CLI** configured with credentials
4. **Tailscale account** (free tier works)
5. **VPC with internet gateway** for instance connectivity

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

allowed_ssh_cidrs = []  # Leave empty to disable SSH, use SSM only

instance_password = "YourSecurePassword123!"
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

### 3. Wait for Instance Initialization

The instance will take approximately 10-15 minutes to fully initialize. The user data script installs all necessary packages and reboots the instance.

Monitor progress:
```bash
# Get instance ID from outputs
terraform output instance_id

# Check system logs
aws ssm send-command \
  --instance-ids $(terraform output -raw instance_id) \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["tail -n 50 /var/log/cloud-init-output.log"]'
```

### 4. Connect via SSM

```bash
# Use the SSM connect command from outputs
aws ssm start-session --target $(terraform output -raw instance_id) --region us-east-1
```

### 5. Activate Tailscale

Once connected via SSM:

```bash
# Start Tailscale and authenticate
sudo tailscale up

# Get your Tailscale IP
tailscale ip -4
```

Follow the authentication URL displayed to authorize the device in your Tailscale account.

### 6. Access Desktop via DCV

Once Tailscale is connected:

1. **Find your Tailscale IP**:
   ```bash
   tailscale ip -4
   ```

2. **Open DCV in your browser**:
   ```
   https://<tailscale-ip>:8443/dcv
   ```

3. **Login credentials**:
   - Username: `ubuntu`
   - Password: The password you set in `terraform.tfvars`

4. **Accept the self-signed certificate** warning (expected on first connection)

## Post-Deployment Configuration

### Syncthing Setup

Syncthing is installed and running for the `ubuntu` user. Access it at:

```
http://<tailscale-ip>:8384
```

Use Syncthing to synchronize browser profiles, bookmarks, and other data between your local machine and the cloud desktop.

### DCV Configuration

The DCV server is configured to:
- Listen on port 8443 with web viewer
- Auto-create console session for `ubuntu` user
- Use system authentication

Configuration file: `/etc/dcv/dcv.conf`

### Tailscale Configuration

To enable additional Tailscale features:

```bash
# Enable SSH over Tailscale
sudo tailscale up --ssh

# Enable MagicDNS
sudo tailscale up --accept-dns

# Set custom hostname
sudo tailscale up --hostname ubuntu-desktop
```

## Maintenance

### Starting/Stopping DCV

```bash
# Check DCV status
sudo systemctl status dcvserver

# Restart DCV
sudo systemctl restart dcvserver

# View DCV sessions
sudo dcv list-sessions
```

### Updating Packages

```bash
sudo apt update
sudo apt upgrade -y
```

### Monitoring Resource Usage

```bash
# CPU and Memory
htop

# Disk usage
df -h

# Network
sudo dcv list-connections
```

## Security Considerations

1. **Tailscale VPN**: All desktop traffic goes through encrypted Tailscale network
2. **No Public Ports**: DCV is not exposed to the internet, only accessible via Tailscale
3. **SSM Access**: Use AWS Systems Manager for secure shell access without public SSH
4. **Encrypted Storage**: Root volume is encrypted at rest
5. **IAM Role**: Minimal permissions (SSM managed instance only)

### Optional: Enable SSH

If you need SSH access for initial setup, add your IP to `allowed_ssh_cidrs`:

```hcl
allowed_ssh_cidrs = ["1.2.3.4/32"]
```

**Recommendation**: Remove SSH access after Tailscale is configured and rely on SSM or Tailscale SSH.

## Outputs

After deployment, Terraform provides:

- `instance_id`: EC2 instance identifier
- `public_ip`: Public IP address (for SSH if enabled)
- `private_ip`: Private IP in VPC
- `ssm_connect_command`: Ready-to-use SSM connection command
- `instance_name`: Instance name tag
- `security_group_id`: Security group identifier

## Troubleshooting

### Instance not accessible via SSM

1. Ensure the subnet has internet access (route to IGW or NAT Gateway)
2. Check IAM role is attached: `aws ec2 describe-instances --instance-ids <id>`
3. Wait a few minutes for SSM agent to register

### DCV not responding

```bash
# Check DCV server status
sudo systemctl status dcvserver

# View DCV logs
sudo cat /var/log/dcv/server.log

# Recreate console session
sudo dcv close-session console
sudo systemctl restart dcvserver
```

### Tailscale not connecting

```bash
# Check Tailscale status
sudo tailscale status

# View logs
sudo journalctl -u tailscaled

# Restart Tailscale
sudo systemctl restart tailscaled
sudo tailscale up
```

### Desktop environment not loading

```bash
# Check display target
systemctl get-default

# Set to graphical
sudo systemctl set-default graphical.target
sudo systemctl isolate graphical.target

# Reboot if needed
sudo reboot
```

## Cost Estimation

Approximate monthly costs (us-east-1, as of 2024):

- **t3.medium instance**: ~$30/month (on-demand)
- **50GB gp3 storage**: ~$4/month
- **Data transfer**: Variable based on usage
- **Tailscale**: Free for personal use (up to 100 devices)

**Total**: ~$34-40/month for the infrastructure

Consider using **Spot instances** or **Reserved instances** for cost savings.

## Cleanup

To destroy all resources:

```bash
terraform destroy
```

Confirm by typing `yes` when prompted.

## Customization

### Change Instance Type

For better performance, modify `instance_type` in `terraform.tfvars`:

```hcl
instance_type = "t3.large"   # 2 vCPU, 8GB RAM
instance_type = "t3.xlarge"  # 4 vCPU, 16GB RAM
```

### Increase Storage

Modify `root_block_device` in `terraform/main.tf`:

```hcl
root_block_device {
  volume_size = 100  # Change to desired size in GB
  volume_type = "gp3"
}
```

### Add Additional Software

Edit the user data script in `terraform/main.tf` under `locals.user_data` to add more packages or configuration.

## References

- [NICE DCV Documentation](https://docs.aws.amazon.com/dcv/)
- [Tailscale Documentation](https://tailscale.com/kb/)
- [AWS Systems Manager](https://docs.aws.amazon.com/systems-manager/)
- [Ubuntu Desktop](https://ubuntu.com/desktop)
- [Syncthing Documentation](https://docs.syncthing.net/)

## License

This infrastructure code is provided as-is for personal or commercial use.

## Support

For issues or questions:
- AWS-specific: Check AWS documentation or support
- Tailscale: Visit Tailscale community forums
- DCV: Reference NICE DCV documentation
- Infrastructure code: Review Terraform logs and AWS console
