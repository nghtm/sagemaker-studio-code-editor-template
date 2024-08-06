import argparse
import yaml


NAME_SUFFIX = 'Fn::Select: [0, Fn::Split: ["-",Fn::Select: [2, Fn::Split: ["/", Ref: "AWS::StackId"]] ]]'


def str_presenter(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, str_presenter)


def generate(input_yaml, output_yaml, default_vpc_lookup_py, lifecycle_config_py, code_editor_py):
    with open(input_yaml, "r") as f:
        template_str = f.read()

    template = yaml.safe_load(template_str.replace("__NAME__SUFFIX__", NAME_SUFFIX))

    with open(default_vpc_lookup_py, "r") as f:
        template["Resources"]["DefaultVpcLookupFunction"]["Properties"]["Code"]["ZipFile"] = f.read()

    with open(lifecycle_config_py, "r") as f:
        template["Resources"]["SageMakerStudioLifecycleConfigFunction"]["Properties"]["Code"]["ZipFile"] = f.read()

    with open(code_editor_py, "r") as f:
        template["Resources"]["SageMakerStudioCodeEditorFunction"]["Properties"]["Code"]["ZipFile"] = f.read()

    with open(output_yaml, "w", encoding="utf-8") as f:
        yaml.dump(template, f, allow_unicode=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_yaml", type=str, default="src/template.yaml")
    parser.add_argument("--output_yaml", type=str, default="CodeEditorStack.template.yaml")
    parser.add_argument("--default_vpc_lookup_py", type=str, default="src/default_vpc_lookup.py")
    parser.add_argument("--lifecycle_config_py", type=str, default="src/lifecycle_config.py")
    parser.add_argument("--code_editor_py", type=str, default="src/code_editor.py")
    args = parser.parse_args()

    generate(
        args.input_yaml,
        args.output_yaml,
        args.default_vpc_lookup_py,
        args.lifecycle_config_py,
        args.code_editor_py,
    )
