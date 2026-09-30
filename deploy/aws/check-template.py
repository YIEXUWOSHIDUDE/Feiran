"""What must stay true of deploy/aws/workbench.yaml, beyond what cfn-lint checks: nothing can
connect in, the container gets no AWS credentials, the data and the backups outlive the host and
the stack, the host cannot delete backups, the registry never expires a tagged release, only a
data volume the stack made may be formatted; and the first-boot script, rendered the way
CloudFormation renders it, is valid bash.

    python deploy/aws/check-template.py deploy/aws/workbench.yaml   (needs PyYAML, e.g. with cfn-lint)
"""

import json
import re
import subprocess
import sys

import yaml


class Loader(yaml.SafeLoader):
    """Reads CloudFormation's short forms (!Ref, !Sub, ...) as {"Ref": ...} and so on."""


def _tagged(loader, suffix, node):
    name = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


Loader.add_multi_constructor("!", _tagged)
SAMPLE = {
    "AWS::AccountId": "123456789012", "AWS::Region": "us-east-1", "LogGroup": "/job-fit-workbench/app",
    "BackupBucket": "sample-bucket", "DeepSeekSecretArn": "",
    "ComposeVersion": "v0.0.0", "ComposeSha256": "0" * 64, "VolumeName": "vol0123456789abcdef0", "VolumeNew": "1",
}


def problems(template: dict) -> list[str]:
    found = []
    resources = template["Resources"]

    def check(condition: bool, problem: str) -> None:
        if not condition:
            found.append(problem)

    check(not [name for name, item in resources.items() if item["Type"] == "AWS::EC2::SecurityGroupIngress"],
          "a security group ingress rule exists")
    group = resources["HostSecurityGroup"]["Properties"]
    check("SecurityGroupIngress" not in group, "the host's security group lets something in")
    check(all((rule["IpProtocol"], rule["FromPort"], rule["ToPort"]) == ("tcp", 443, 443) for rule in group["SecurityGroupEgress"]),
          "the host may send more than HTTPS")
    host = resources["Host"]["Properties"]
    check("KeyName" not in host, "the host has an SSH key")
    metadata = host.get("MetadataOptions", {})
    check(metadata.get("HttpTokens") == "required" and metadata.get("HttpPutResponseHopLimit") == 1,
          "the metadata service is not IMDSv2 with one hop, so a container could take the host's credentials")
    check(all(mapping["Ebs"].get("Encrypted") is True for mapping in host["BlockDeviceMappings"]), "the root disk is not encrypted")
    volume = resources["DataVolume"]
    check(volume.get("DeletionPolicy") == "Retain" and volume.get("UpdateReplacePolicy") == "Retain",
          "the data volume does not outlive the stack")
    check(volume["Properties"].get("Encrypted") is True, "the data volume is not encrypted")
    bucket = resources["BackupBucket"]
    check(bucket.get("DeletionPolicy") == "Retain" and bucket.get("UpdateReplacePolicy") == "Retain",
          "the backups do not outlive the stack")
    blocked = bucket["Properties"].get("PublicAccessBlockConfiguration", {})
    check(all(blocked.get(key) is True for key in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets")),
          "the backup bucket does not block public access")
    check("BucketEncryption" in bucket["Properties"], "the backup bucket is not encrypted")
    check(any("ExpirationInDays" in rule for rule in bucket["Properties"]["LifecycleConfiguration"]["Rules"]),
          "backups are kept forever")
    statements = [statement for policy in resources["HostRole"]["Properties"]["Policies"]
                  for statement in policy["PolicyDocument"]["Statement"] if isinstance(statement, dict) and "Action" in statement]
    actions = {action for statement in statements
               for action in ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])}
    check(not {action for action in actions if action == "*" or action.endswith(":*") or "Delete" in action
               and not action.startswith("ec2messages:")},  # the agent deletes its own messages
          f"the host may delete or do anything: {sorted(actions)}")
    check("ManagedPolicyArns" not in resources["HostRole"]["Properties"], "the host role takes a managed policy's wider rights")
    rules = json.loads(resources["Repository"]["Properties"]["LifecyclePolicy"]["LifecyclePolicyText"])["rules"]
    check(all(rule["selection"]["tagStatus"] == "untagged" for rule in rules),
          "the registry can expire a tagged release, which may be the one running or the one to go back to")
    values = resources["Host"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"][1]
    check(values.get("VolumeNew") == {"Fn::If": ["CreateDataVolume", "1", "0"]},
          "the host may format a data volume the stack did not make (one given as DataVolumeId holds data)")
    return found


def user_data(template: dict) -> str:
    """The first-boot script as CloudFormation's Fn::Sub renders it, with sample values."""
    text = template["Resources"]["Host"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"][0]

    def value(match: re.Match) -> str:
        name = match.group(1)
        return "${" + name[1:] + "}" if name.startswith("!") else SAMPLE[name]

    return re.sub(r"\$\{([^}]+)\}", value, text)


def main() -> int:
    template = yaml.load(open(sys.argv[1], encoding="utf-8"), Loader=Loader)
    found = problems(template)
    script = user_data(template)
    parsed = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    if parsed.returncode:
        found.append(f"the first-boot script is not valid bash: {parsed.stderr.strip()}")
    for problem in found:
        print(f"problem: {problem}")
    print("template: all checks passed" if not found else f"template: {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
