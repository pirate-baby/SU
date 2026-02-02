terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Data source to get latest Ubuntu 22.04 AMI
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# IAM Role for SSM access
resource "aws_iam_role" "ubuntu_desktop_role" {
  name = "${var.instance_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.instance_name}-role"
  }
}

# Attach SSM managed instance policy
resource "aws_iam_role_policy_attachment" "ssm_policy" {
  role       = aws_iam_role.ubuntu_desktop_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# IAM Instance Profile
resource "aws_iam_instance_profile" "ubuntu_desktop_profile" {
  name = "${var.instance_name}-profile"
  role = aws_iam_role.ubuntu_desktop_role.name
}

# Security Group
resource "aws_security_group" "ubuntu_desktop_sg" {
  name        = "${var.instance_name}-sg"
  description = "Security group for Ubuntu Desktop with Tailscale"
  vpc_id      = var.vpc_id

  # SSH access for initial setup
  dynamic "ingress" {
    for_each = var.allowed_ssh_cidrs
    content {
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
      description = "SSH access"
    }
  }

  # Allow all outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound"
  }

  tags = {
    Name = "${var.instance_name}-sg"
  }
}

# User data script
locals {
  user_data = <<-EOF
    #!/bin/bash
    set -e

    # Update system
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get upgrade -y

    # Install Ubuntu Desktop (minimal)
    apt-get install -y ubuntu-desktop-minimal

    # Install Tailscale
    curl -fsSL https://tailscale.com/install.sh | sh

    # Install NICE DCV Server
    wget https://d1uj6qtbmh3dt5.cloudfront.net/NICE-GPG-KEY
    gpg --import NICE-GPG-KEY

    # Download and install DCV server
    wget https://d1uj6qtbmh3dt5.cloudfront.net/2023.1/Servers/nice-dcv-2023.1-16388-ubuntu2204-x86_64.tgz
    tar -xvzf nice-dcv-2023.1-16388-ubuntu2204-x86_64.tgz
    cd nice-dcv-2023.1-16388-ubuntu2204-x86_64
    apt-get install -y ./nice-dcv-server_2023.1.16388-1_amd64.ubuntu2204.deb
    apt-get install -y ./nice-dcv-web-viewer_2023.1.16388-1_amd64.ubuntu2204.deb
    apt-get install -y ./nice-xdcv_2023.1.565-1_amd64.ubuntu2204.deb
    cd ..
    rm -rf nice-dcv-2023.1-16388-ubuntu2204-x86_64*

    # Install Google Chrome
    wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y ./google-chrome-stable_current_amd64.deb
    rm google-chrome-stable_current_amd64.deb

    # Install Syncthing
    curl -s https://syncthing.net/release-key.txt | apt-key add -
    echo "deb https://apt.syncthing.net/ syncthing stable" | tee /etc/apt/sources.list.d/syncthing.list
    apt-get update
    apt-get install -y syncthing

    # Set ubuntu user password
    echo "ubuntu:${var.instance_password}" | chpasswd

    # Configure DCV
    # Enable console session for ubuntu user
    cat > /etc/dcv/dcv.conf << 'DCVCONF'
    [license]
    [log]
    level=info

    [session-management]
    create-session = true

    [session-management/defaults]

    [session-management/automatic-console-session]
    owner = "ubuntu"

    [display]

    [connectivity]
    enable-quic-frontend=true
    web-port=8443
    web-url-path="/dcv"

    [security]
    authentication="system"
    DCVCONF

    # Set up systemd service for Syncthing (ubuntu user)
    systemctl enable syncthing@ubuntu.service
    systemctl start syncthing@ubuntu.service

    # Enable and start DCV server
    systemctl enable dcvserver
    systemctl start dcvserver

    # Create a console session
    systemctl isolate graphical.target

    # Reboot to ensure all services start properly
    reboot
  EOF
}

# EC2 Instance
resource "aws_instance" "ubuntu_desktop" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.ubuntu_desktop_sg.id]
  key_name               = var.key_name
  iam_instance_profile   = aws_iam_instance_profile.ubuntu_desktop_profile.name

  root_block_device {
    volume_size           = 50
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true

    tags = {
      Name = "${var.instance_name}-root"
    }
  }

  user_data = local.user_data

  tags = {
    Name = var.instance_name
  }

  lifecycle {
    ignore_changes = [
      ami,
      user_data
    ]
  }
}
