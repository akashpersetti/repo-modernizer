# infra/frontend.tf
resource "aws_s3_bucket" "frontend" {
  bucket = "repomodernizer-frontend-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "repomod-frontend-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# CloudFront+OAC uses S3's REST API origin, not the S3 website-hosting endpoint --
# it does NOT auto-resolve an extensionless request path like "/task" to "task.html"
# the way S3 website hosting would. Next.js static export produces task.html for
# the /task route. A CloudFront Function rewrites the viewer-request URI before
# it hits the origin: "/task" -> "/task.html", "/" -> "/index.html". Without this,
# navigating straight to /task?id=X (exactly what the home page's redirect does)
# would 403/404 against S3.
resource "aws_cloudfront_function" "url_rewrite" {
  name    = "repomod-frontend-url-rewrite"
  runtime = "cloudfront-js-2.0"
  comment = "append .html to extensionless paths for Next.js static export"
  publish = true
  code    = <<-EOT
    function handler(event) {
      var request = event.request;
      var uri = request.uri;

      if (uri.endsWith('/')) {
        request.uri = uri + 'index.html';
      } else if (!uri.includes('.')) {
        request.uri = uri + '.html';
      }
      return request;
    }
  EOT
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  default_root_object = "index.html"
  comment              = "repomod-frontend"

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "frontend-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD"]
    cached_methods          = ["GET", "HEAD"]
    target_origin_id       = "frontend-s3"
    viewer_protocol_policy = "redirect-to-https"

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.url_rewrite.arn
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontServicePrincipal"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.frontend.arn
        }
      }
    }]
  })
}

output "dashboard_url" {
  value = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "dashboard_bucket_name" {
  value = aws_s3_bucket.frontend.bucket
}

output "dashboard_distribution_id" {
  value = aws_cloudfront_distribution.frontend.id
}
