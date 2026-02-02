variable "aws_region" {
  description = "AWS region where resources will be created"
  type        = string
  default     = "us-east-1"
}

variable "vpc_id" {
  description = "VPC ID where the instance will be launched"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID where the instance will be launched (should have internet access)"
  type        = string
}

variable "key_name" {
  description = "EC2 key pair name for SSH access"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.medium"
}

variable "instance_name" {
  description = "Name tag for the EC2 instance"
  type        = string
  default     = "ubuntu-desktop-tailscale"
}

variable "allowed_ssh_cidrs" {
  description = "List of CIDR blocks allowed to SSH to the instance"
  type        = list(string)
  default     = []
}

variable "instance_password" {
  description = "Password for the ubuntu user"
  type        = string
  sensitive   = true
}
