import json
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_secret_example_keys_match_ecs_injection() -> None:
    example_path = ROOT / "deploy/mia-prod.secret.example.json"
    task_path = ROOT / "deploy/ecs-task-definition.example.json"
    example = json.loads(example_path.read_text(encoding="utf-8"))
    task = json.loads(task_path.read_text(encoding="utf-8"))
    secrets = task["containerDefinitions"][0]["secrets"]
    names = [row["name"] for row in secrets]
    assert names == list(example.keys())
    for row in secrets:
        assert row["valueFrom"].endswith(f":{row['name']}::")
    assert example["MIA_OPENAI_API_KEY"] == ""
    assert "sslmode=verify-full" in example["MIA_DATABASE_URL"]
    assert "rds-global-bundle.pem" in example["MIA_DATABASE_URL"]
    assert example["MIA_APIFY_TOKEN"] == ""
    assert "MIA_LINKEDIN_ACCESS_TOKEN" not in example
    assert "MIA_META_ADS_ACCOUNT_ID" not in example


def test_env_example_documents_settings_and_adapter_map() -> None:
    from app.core.config import Settings

    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    missing = [
        f"MIA_{name.upper()}"
        for name in Settings.model_fields
        if f"MIA_{name.upper()}" not in text
    ]
    assert missing == []
    assert "MIA_INSTAGRAM_SENDER=direct" in text
    assert "MIA_INSTAGRAM_SENDER=composio" not in text
    assert "MIA_WHATSAPP_SENDER=composio" in text
    assert "MIA_WHATSAPP_SENDER=direct" not in text
    assert "MIA_META_ADS_ACCOUNT_ID" not in text
    assert "MIA_LINKEDIN_ACCESS_TOKEN" not in text
    assert "MIA_APIFY_TOKEN=" in text
    assert "ADR-015" in text
    assert not (ROOT / "deploy/Caddyfile").exists()
    assert not (ROOT / "deploy/mia.service").exists()
    production = (ROOT / "docs/PRODUCTION_BUILD.md").read_text(encoding="utf-8")
    assert "deploy/Caddyfile" not in production
    assert "mia.service" not in production


def test_ecs_service_example_is_private_fargate() -> None:
    service = json.loads((ROOT / "deploy/ecs-service.example.json").read_text(encoding="utf-8"))
    net = service["networkConfiguration"]["awsvpcConfiguration"]
    assert service["launchType"] == "FARGATE"
    assert service["platformVersion"] == "1.4.0"
    assert service["enableExecuteCommand"] is False
    assert service["healthCheckGracePeriodSeconds"] == 120
    breaker = service["deploymentConfiguration"]["deploymentCircuitBreaker"]
    assert breaker == {"enable": True, "rollback": True}
    assert net["assignPublicIp"] == "DISABLED"
    assert service["loadBalancers"][0]["containerPort"] == 8000
    assert service["loadBalancers"][0]["containerName"] == "mia"
    overrides = json.loads(
        (ROOT / "deploy/ecs-migrate-overrides.example.json").read_text(encoding="utf-8")
    )
    assert overrides["containerOverrides"][0]["command"] == ["mia-migrate"]
    due_path = ROOT / "deploy/ecs-due-scan-overrides.example.json"
    recon_path = ROOT / "deploy/ecs-reconcile-overrides.example.json"
    due = json.loads(due_path.read_text(encoding="utf-8"))
    recon = json.loads(recon_path.read_text(encoding="utf-8"))
    assert due["containerOverrides"][0]["command"] == ["mia-due-scan"]
    assert recon["containerOverrides"][0]["command"] == ["mia-reconcile"]
    task_path = ROOT / "deploy/ecs-task-definition.example.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    check = task["containerDefinitions"][0]["healthCheck"]
    assert task["containerDefinitions"][0]["stopTimeout"] == 60
    assert check["startPeriod"] == 90
    assert "timeout=4" in check["command"][1]
    assert "/health/live" in check["command"][1]


def test_pyproject_readme_is_inline_for_docker_context() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    readme = data["project"]["readme"]
    assert isinstance(readme, dict)
    assert "text" in readme
    dockerfile = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")
    assert "docs/PRD.md" not in dockerfile
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "docs/" in dockerignore
    assert "chmod 644 /etc/ssl/certs/rds-global-bundle.pem" in dockerfile
    assert "--proxy-headers" in dockerfile
    assert "--forwarded-allow-ips" in dockerfile
    assert '"--timeout-keep-alive", "130"' in dockerfile
    assert "PYTHONUNBUFFERED=1" in dockerfile
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "apify-client" not in pyproject
    assert "apify>=" not in pyproject


def test_ci_builds_production_image() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "docker build -f deploy/Dockerfile -t mia:ci ." in workflow


def test_eventbridge_schedules_are_private_persist_only() -> None:
    due = json.loads(
        (ROOT / "deploy/eventbridge-due-scan.example.json").read_text(encoding="utf-8")
    )
    recon = json.loads(
        (ROOT / "deploy/eventbridge-reconcile.example.json").read_text(encoding="utf-8")
    )
    due_overrides = json.loads(
        (ROOT / "deploy/ecs-due-scan-overrides.example.json").read_text(encoding="utf-8")
    )
    recon_overrides = json.loads(
        (ROOT / "deploy/ecs-reconcile-overrides.example.json").read_text(encoding="utf-8")
    )
    iam = json.loads((ROOT / "deploy/iam-scheduler-ecs.example.json").read_text(encoding="utf-8"))
    assert due["ScheduleExpression"] == "rate(15 minutes)"
    assert recon["ScheduleExpression"] == "rate(1 hour)"
    for schedule in (due, recon):
        ecs = schedule["Target"]["EcsParameters"]
        net = ecs["NetworkConfiguration"]["awsvpcConfiguration"]
        assert ecs["LaunchType"] == "FARGATE"
        assert ecs["PlatformVersion"] == "1.4.0"
        assert ecs["EnableExecuteCommand"] is False
        assert net["AssignPublicIp"] == "DISABLED"
        assert schedule["FlexibleTimeWindow"]["Mode"] == "OFF"
        assert schedule["Target"]["RetryPolicy"]["MaximumRetryAttempts"] == 0
    assert json.loads(due["Target"]["Input"]) == due_overrides
    assert json.loads(recon["Target"]["Input"]) == recon_overrides
    actions = {stmt["Sid"]: stmt for stmt in iam["Statement"]}
    assert actions["RunMiaTaskOnMiaCluster"]["Action"] == "ecs:RunTask"
    assert actions["PassMiaTaskRoles"]["Action"] == "iam:PassRole"
    assert actions["PassMiaTaskRoles"]["Condition"]["StringEquals"]["iam:PassedToService"] == (
        "ecs-tasks.amazonaws.com"
    )
    ecs_trust = json.loads(
        (ROOT / "deploy/iam-ecs-task-trust.example.json").read_text(encoding="utf-8")
    )
    sched_trust = json.loads(
        (ROOT / "deploy/iam-scheduler-trust.example.json").read_text(encoding="utf-8")
    )
    assert ecs_trust["Statement"][0]["Principal"]["Service"] == "ecs-tasks.amazonaws.com"
    assert sched_trust["Statement"][0]["Principal"]["Service"] == "scheduler.amazonaws.com"
    assert sched_trust["Statement"][0]["Condition"]["StringEquals"]["aws:SourceAccount"] == (
        "ACCOUNT_ID"
    )


def test_alb_examples_are_https_ip_targets() -> None:
    alb = json.loads((ROOT / "deploy/alb.example.json").read_text(encoding="utf-8"))
    tg = json.loads((ROOT / "deploy/alb-target-group.example.json").read_text(encoding="utf-8"))
    attrs = json.loads(
        (ROOT / "deploy/alb-target-group-attributes.example.json").read_text(encoding="utf-8")
    )
    alb_attrs = json.loads(
        (ROOT / "deploy/alb-attributes.example.json").read_text(encoding="utf-8")
    )
    https = json.loads(
        (ROOT / "deploy/alb-listener-https.example.json").read_text(encoding="utf-8")
    )
    http = json.loads(
        (ROOT / "deploy/alb-listener-http-redirect.example.json").read_text(encoding="utf-8")
    )
    assert alb["Scheme"] == "internet-facing"
    assert alb["Type"] == "application"
    assert alb["Subnets"] == ["subnet-PUBLIC_A", "subnet-PUBLIC_B"]
    assert tg["TargetType"] == "ip"
    assert tg["Protocol"] == "HTTP"
    assert tg["Port"] == 8000
    assert tg["HealthCheckPath"] == "/health/live"
    assert tg["Matcher"]["HttpCode"] == "200"
    assert attrs["Attributes"][0] == {
        "Key": "deregistration_delay.timeout_seconds",
        "Value": "30",
    }
    assert alb_attrs["Attributes"][0] == {
        "Key": "idle_timeout.timeout_seconds",
        "Value": "120",
    }
    keep_alive = 130
    idle = int(alb_attrs["Attributes"][0]["Value"])
    assert keep_alive > idle
    assert https["Protocol"] == "HTTPS"
    assert https["Port"] == 443
    assert https["SslPolicy"] == "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09"
    assert https["DefaultActions"][0]["Type"] == "forward"
    assert http["Protocol"] == "HTTP"
    assert http["Port"] == 80
    redirect = http["DefaultActions"][0]
    assert redirect["Type"] == "redirect"
    assert redirect["RedirectConfig"]["StatusCode"] == "HTTP_301"
    assert redirect["RedirectConfig"]["Protocol"] == "HTTPS"
    assert redirect["RedirectConfig"]["Port"] == "443"


def test_production_build_creates_alb_then_migrate_then_service() -> None:
    text = (ROOT / "docs/PRODUCTION_BUILD.md").read_text(encoding="utf-8")
    request_cert = text.index("aws acm request-certificate")
    create_exec_role = text.index(
        "aws iam create-role --role-name miaTaskExecutionRole"
    )
    register = text.index("aws ecs register-task-definition")
    wait_alb = text.index("aws elbv2 wait load-balancer-available")
    create_https = text.index(
        "aws elbv2 create-listener --cli-input-json file://./deploy/local/alb-listener-https.json"
    )
    create_tg = text.index("aws elbv2 create-target-group")
    run_migrate = text.index("aws ecs run-task")
    create_svc = text.index("aws ecs create-service")
    assert create_exec_role < register
    assert wait_alb < create_https
    assert request_cert < create_tg < run_migrate < create_svc
    assert text.index("aws ec2 create-security-group") < text.index(
        "aws elbv2 create-load-balancer"
    )
    assert text.index("aws acm wait certificate-validated") < create_https
    assert text.index("aws ecs wait tasks-stopped") < create_svc
    assert text.index("aws ecs wait tasks-stopped") < text.index(
        'tasks[0].containers[0].exitCode'
    )
    assert text.index('tasks[0].containers[0].exitCode') < create_svc
    assert "AWSCLIV2-User.msi" in text
    assert "Amazon.AWSCLI" in text
    assert "aws login" in text
    assert text.index("assert-aws-identity.ps1") < text.index("VPC and more")
    assert "console.aws.amazon.com/vpc/home?region=eu-north-1" in text
    assert text.index("assert-aws-identity.ps1") < text.index(
        "aws ec2 create-security-group"
    )
    assert "eu-north-1.console.aws.amazon.com/vpc/home?region=eu-north-1" in text
    assert text.index(
        "eu-north-1.console.aws.amazon.com/vpc/home?region=eu-north-1"
    ) < text.index("aws ec2 create-security-group")
    assert text.index("aws ecs wait services-stable") > create_svc
    assert "skips `create_all`" in text
    assert text.index("aws ec2 create-security-group") < text.index(
        "deploy/fill-placeholders.ps1"
    )
    assert text.index("deploy/fill-placeholders.ps1") < text.index(
        "aws ec2 authorize-security-group-ingress"
    )
    assert text.index("assert-local-stamped.ps1 -Stage network") < text.index(
        "aws ec2 authorize-security-group-ingress"
    )
    between_tg_and_https = text[create_tg:create_https]
    assert "fill-placeholders.ps1" in between_tg_and_https
    assert "assert-local-stamped.ps1 -Stage alb" in between_tg_and_https
    assert text.index("aws rds wait db-instance-available") < text.index(
        "MasterUserSecret.SecretArn"
    )
    assert "--query GroupId" in text
    cli_files = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("aws ") and "file://" in line
    ]
    assert cli_files
    for line in cli_files:
        assert "file://./deploy/local/" in line
        assert ".example.json" not in line
    assert "$env:AWS_DEFAULT_REGION" in text
    assert "list-hosted-zones-by-name" in text
    alb_sg = json.loads(
        (ROOT / "deploy/sg-alb-ingress.example.json").read_text(encoding="utf-8")
    )
    task_sg = json.loads(
        (ROOT / "deploy/sg-tasks-ingress.example.json").read_text(encoding="utf-8")
    )
    rds_sg = json.loads(
        (ROOT / "deploy/sg-rds-ingress.example.json").read_text(encoding="utf-8")
    )
    alb_ports = {row["FromPort"] for row in alb_sg["IpPermissions"]}
    assert alb_ports == {80, 443}
    assert task_sg["IpPermissions"][0]["FromPort"] == 8000
    assert task_sg["IpPermissions"][0]["UserIdGroupPairs"][0]["GroupId"] == "sg-MIA_ALB"
    assert "IpRanges" not in task_sg["IpPermissions"][0]
    assert rds_sg["IpPermissions"][0]["FromPort"] == 5432
    assert rds_sg["IpPermissions"][0]["UserIdGroupPairs"][0]["GroupId"] == "sg-MIA_TASKS"
    assert "IpRanges" not in rds_sg["IpPermissions"][0]
    acm = json.loads(
        (ROOT / "deploy/acm-certificate.example.json").read_text(encoding="utf-8")
    )
    assert acm["DomainName"] == "mia.assafweb.com"
    assert acm["ValidationMethod"] == "DNS"
    assert text.index("aws rds create-db-subnet-group") < text.index(
        "aws rds create-db-instance"
    )
    assert text.index("aws rds wait db-instance-available") < text.index(
        "aws acm request-certificate"
    )
    assert text.index("aws elbv2 create-load-balancer") < text.index(
        "aws route53 change-resource-record-sets"
    )
    rds = json.loads((ROOT / "deploy/rds.example.json").read_text(encoding="utf-8"))
    assert rds["Engine"] == "postgres"
    assert rds["EngineVersion"] == "16"
    assert rds["PubliclyAccessible"] is False
    assert rds["StorageEncrypted"] is True
    assert rds["ManageMasterUserPassword"] is True
    assert "MasterUserPassword" not in rds
    dns = json.loads((ROOT / "deploy/route53-mia.example.json").read_text(encoding="utf-8"))
    record = dns["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
    assert record["Name"] == "mia.assafweb.com"
    assert record["Type"] == "A"
    assert "VPC and more" in text
    assert "DNS hostnames" in text
    assert text.index("aws ecs wait services-stable") < text.index(
        "aws cloudwatch put-metric-alarm"
    )
    unhealthy = json.loads(
        (ROOT / "deploy/cloudwatch-alb-unhealthy.example.json").read_text(encoding="utf-8")
    )
    five = json.loads(
        (ROOT / "deploy/cloudwatch-alb-5xx.example.json").read_text(encoding="utf-8")
    )
    assert unhealthy["MetricName"] == "UnHealthyHostCount"
    assert unhealthy["Statistic"] == "Minimum"
    assert five["MetricName"] == "HTTPCode_ELB_5XX_Count"
    assert "AlarmActions" not in unhealthy
    assert five["AlarmActions"] == [
        "arn:aws:sns:REGION:ACCOUNT_ID:mia-alb-5xx"
    ]
    example_env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "MIA_ALB_5XX_SNS_TOPIC_ARN=" in example_env
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "scripts/assert_origin_bind.py" in ci
    assert "workflow_dispatch" in ci
    deploy_script = (ROOT / "scripts/deploy_ecs_revision.py").read_text(encoding="utf-8")
    assert "assert_origin_bind.py" in deploy_script


def test_fill_placeholders_script_skips_secrets_and_aws() -> None:
    script = (ROOT / "deploy/fill-placeholders.ps1").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "deploy/local/" in gitignore
    assert "mia-prod.secret.example.json" in script
    assert "aws " not in script
    assert ".env" not in script
    assert "Get-Content" not in script
    assert "Invoke-Expression" not in script
    build = (ROOT / "docs/PRODUCTION_BUILD.md").read_text(encoding="utf-8")
    assert "deploy/fill-placeholders.ps1" in build
    assert "file://./deploy/local/" in script
    assert_script = (ROOT / "deploy/assert-local-stamped.ps1").read_text(encoding="utf-8")
    assert "aws " not in assert_script
    assert ".env" not in assert_script
    assert "Get-Content" not in assert_script
    assert "Invoke-Expression" not in assert_script
    assert "assert-local-stamped.ps1" in build


def test_fill_placeholders_replaces_tokens_on_windows() -> None:
    import subprocess
    import sys

    if sys.platform != "win32":
        return
    script = ROOT / "deploy/fill-placeholders.ps1"
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-AccountId",
            "111122223333",
            "-Region",
            "il-central-1",
            "-VpcId",
            "vpc-aabbccdd",
            "-AlbHash",
            "50dc6c495c0c9188",
            "-TargetGroupHash",
            "0123456789abcdef",
        ],
        check=True,
        cwd=ROOT,
    )
    task = json.loads(
        (ROOT / "deploy/local/ecs-task-definition.json").read_text(encoding="utf-8")
    )
    assert "111122223333" in task["executionRoleArn"]
    secrets = {
        item["name"]: item["valueFrom"]
        for item in task["containerDefinitions"][0]["secrets"]
    }
    assert secrets["MIA_INSTAGRAM_ACCOUNT_ID"].endswith(
        "mia/prod:MIA_INSTAGRAM_ACCOUNT_ID::"
    )
    assert task["containerDefinitions"][0]["logConfiguration"]["options"][
        "awslogs-region"
    ] == "il-central-1"
    tg = json.loads(
        (ROOT / "deploy/local/alb-target-group.json").read_text(encoding="utf-8")
    )
    assert tg["VpcId"] == "vpc-aabbccdd"
    unhealthy = json.loads(
        (ROOT / "deploy/local/cloudwatch-alb-unhealthy.json").read_text(encoding="utf-8")
    )
    five = json.loads(
        (ROOT / "deploy/local/cloudwatch-alb-5xx.json").read_text(encoding="utf-8")
    )
    assert unhealthy["Dimensions"][0]["Value"] == "app/mia/50dc6c495c0c9188"
    assert unhealthy["Dimensions"][1]["Value"] == "targetgroup/mia/0123456789abcdef"
    assert five["Dimensions"][0]["Value"] == "app/mia/50dc6c495c0c9188"
    assert "HASH" not in json.dumps(unhealthy)
    assert not (ROOT / "deploy/local/mia-prod.secret.json").exists()


def _run_ps1(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_assert_local_stamped_stages_on_windows() -> None:
    import sys

    if sys.platform != "win32":
        return
    fill = ROOT / "deploy/fill-placeholders.ps1"
    stamp = ROOT / "deploy/assert-local-stamped.ps1"
    incomplete = _run_ps1(
        fill,
        "-AccountId",
        "111122223333",
        "-Region",
        "il-central-1",
        "-VpcId",
        "vpc-aabbccdd",
    )
    assert incomplete.returncode == 0
    network_fail = _run_ps1(stamp, "-Stage", "network")
    assert network_fail.returncode == 1
    network_ok = _run_ps1(
        fill,
        "-AccountId",
        "111122223333",
        "-Region",
        "il-central-1",
        "-VpcId",
        "vpc-aabbccdd",
        "-SubnetPublicA",
        "subnet-pub-a",
        "-SubnetPublicB",
        "subnet-pub-b",
        "-SubnetPrivateA",
        "subnet-priv-a",
        "-SubnetPrivateB",
        "subnet-priv-b",
        "-SgAlb",
        "sg-alb1",
        "-SgTasks",
        "sg-tasks1",
        "-SgRds",
        "sg-rds1",
    )
    assert network_ok.returncode == 0
    assert _run_ps1(stamp, "-Stage", "network").returncode == 0
    assert _run_ps1(stamp, "-Stage", "alb").returncode == 1
    alb_ok = _run_ps1(
        fill,
        "-AccountId",
        "111122223333",
        "-Region",
        "il-central-1",
        "-VpcId",
        "vpc-aabbccdd",
        "-SubnetPublicA",
        "subnet-pub-a",
        "-SubnetPublicB",
        "subnet-pub-b",
        "-SubnetPrivateA",
        "subnet-priv-a",
        "-SubnetPrivateB",
        "subnet-priv-b",
        "-SgAlb",
        "sg-alb1",
        "-SgTasks",
        "sg-tasks1",
        "-SgRds",
        "sg-rds1",
        "-AlbHash",
        "50dc6c495c0c9188",
        "-TargetGroupHash",
        "0123456789abcdef",
        "-CertId",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "-Route53ZoneId",
        "ZABCDEF",
        "-AlbCanonicalHostedZoneId",
        "ZCANON",
        "-AlbDnsName",
        "mia-123.il-central-1.elb.amazonaws.com",
    )
    assert alb_ok.returncode == 0
    assert _run_ps1(stamp, "-Stage", "alb").returncode == 0


def test_assert_aws_identity_script_is_sts_only() -> None:
    script = (ROOT / "deploy/assert-aws-identity.ps1").read_text(encoding="utf-8")
    build = (ROOT / "docs/PRODUCTION_BUILD.md").read_text(encoding="utf-8")
    lower = script.lower()
    assert ".env" not in script
    assert "Get-Content" not in script
    assert "create-vpc" not in lower
    assert "create-nat" not in lower
    assert "sts get-caller-identity" in script
    assert "eu-north-1" in script
    assert "assert-aws-identity.ps1" in build


def test_assert_aws_identity_gate_on_windows() -> None:
    import sys

    if sys.platform != "win32":
        return
    result = _run_ps1(ROOT / "deploy/assert-aws-identity.ps1")
    combined = f"{result.stdout}{result.stderr}"
    if result.returncode == 0:
        account = result.stdout.strip()
        assert account.isdigit() and len(account) == 12
        return
    assert result.returncode == 1
    fail_closed = (
        "aws is not on PATH" in combined
        or "aws login" in combined
        or "sts did not return a 12-digit account id" in combined
    )
    assert fail_closed, combined

