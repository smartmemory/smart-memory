"""Normalize arbitrary add properties before Click chooses positional text."""

import click


class AddCommand(click.Command):
    def parse_args(self, ctx, args):
        known = {
            option: parameter
            for parameter in self.get_params(ctx)
            if isinstance(parameter, click.Option)
            for option in (*parameter.opts, *parameter.secondary_opts)
        }
        normalized = []
        positionals = []
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--":
                normalized.extend(args[index:])
                positionals.extend(args[index + 1 :])
                break
            option, equal, value = token.partition("=")
            if option in known:
                normalized.append(token)
                parameter = known[option]
                if not equal and not parameter.is_flag and not parameter.count:
                    normalized.extend(args[index + 1 : index + 1 + parameter.nargs])
                    index += parameter.nargs
            elif token.startswith("--"):
                key = option[2:]
                if not equal:
                    if index + 1 >= len(args) or args[index + 1].startswith("--"):
                        raise click.ClickException(
                            f"Property {option} requires a value (missing value)."
                        )
                    index += 1
                    value = args[index]
                normalized.extend(["--prop", f"{key}={value}"])
            else:
                normalized.append(token)
                positionals.append(token)
            index += 1
        if len(positionals) > 1:
            raise click.UsageError(
                "Multiple TEXT words. Quote the text or use --all - with stdin.", ctx
            )
        return super().parse_args(ctx, normalized)
