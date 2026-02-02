output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.ubuntu_desktop.id
}

output "public_ip" {
  description = "Public IP address of the instance"
  value       = aws_instance.ubuntu_desktop.public_ip
}

output "private_ip" {
  description = "Private IP address of the instance"
  value       = aws_instance.ubuntu_desktop.private_ip
}

output "ssm_connect_command" {
  description = "AWS Systems Manager Session Manager connect command"
  value       = "aws ssm start-session --target ${aws_instance.ubuntu_desktop.id} --region ${var.aws_region}"
}

output "instance_name" {
  description = "Name of the instance"
  value       = var.instance_name
}

output "security_group_id" {
  description = "Security group ID"
  value       = aws_security_group.ubuntu_desktop_sg.id
}
