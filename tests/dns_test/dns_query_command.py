import base64
import shlex


def build_python_script_argv(script, args, python_executable="python3"):
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    launcher = (
        "import base64,sys;"
        "code=sys.argv[1];"
        "sys.argv=[sys.argv[0]]+sys.argv[2:];"
        "exec(base64.b64decode(code).decode('utf-8'))"
    )
    return [python_executable, "-c", launcher, encoded] + list(args)


def build_python_script_command(script, args, python_executable="python3"):
    argv = build_python_script_argv(script, args, python_executable)
    return " ".join(shlex.quote(value) for value in argv)
