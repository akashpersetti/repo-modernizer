terraform {
  backend "s3" {
    bucket         = "repomodernizer-tfstate-914697327092"
    key            = "repomodernizer/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "repomod-tf-lock"
    encrypt        = true
  }
}
