"""Private isolated JSON Schema worker. Protocol output contains no customer content."""

# Executed by absolute path with Python -I and an empty environment. Do not
# import application modules, resolve credentials, or enable network references.
import json
import sys


def main():
    # Wall-clock timeout and reaping belong to the parent. Linux also has a
    # hard address-space bound; this is process isolation, not a Python sandbox.
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    from jsonschema import Draft202012Validator, SchemaError, ValidationError
    from referencing import Registry
    from referencing.exceptions import NoSuchResource, Unresolvable

    def reject_retrieval(uri):
        raise NoSuchResource(ref=uri)

    registry = Registry(retrieve=reject_retrieval)
    try:
        raw = sys.stdin.buffer.read(2_000_129)
        if len(raw) > 2_000_128:
            sys.stdout.write("json")
            return
        request = json.loads(raw)
        schema = request["schema"]
        Draft202012Validator.check_schema(schema)
        if request["mode"] == "validate":
            Draft202012Validator(schema, registry=registry).validate(request["instance"])
        elif request["mode"] != "schema":
            raise ValueError
    except SchemaError:
        sys.stdout.write("schema")
    except ValidationError:
        sys.stdout.write("instance")
    except Unresolvable:
        sys.stdout.write("reference")
    except Exception:
        # Unexpected runtime/resource failures are not malformed customer input.
        # No exception rendering, traceback or instance/schema echo.
        sys.stdout.write("unavailable")
    else:
        sys.stdout.write("valid")


if __name__ == "__main__":
    main()
