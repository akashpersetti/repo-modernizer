resource "aws_efs_file_system" "workspace" {
  encrypted = true
}

resource "aws_efs_mount_target" "workspace" {
  count           = length(aws_subnet.public)
  file_system_id  = aws_efs_file_system.workspace.id
  subnet_id       = aws_subnet.public[count.index].id
  security_groups = [aws_security_group.worker.id]
}

resource "aws_efs_access_point" "workspace" {
  file_system_id = aws_efs_file_system.workspace.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/workspace"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "755"
    }
  }
}
