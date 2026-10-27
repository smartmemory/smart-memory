"""Normalize arbitrary property flags before Click chooses the positional argument."""

import re

import click

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CLOCK = re.compile(r"\d{2}:\d{2}(:\d{2}(\.\d+)?)?([zZ]|[+-]\d{2}:?\d{2})?")


class PropertyCommand(click.Command):
    """Command whose unknown ``--key value`` flags become ``--prop key=value``.

    The rewrite runs before Click binds the positional, so a property flag may sit
    before or after it. ``max_positionals`` is the count the command accepts.
    """

    max_positionals = 1
    positional_error = "Multiple arguments. Quote the value."

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
        if len(positionals) > self.max_positionals:
            raise click.UsageError(self.positional_error, ctx)
        return super().parse_args(ctx, normalized)


class AddCommand(PropertyCommand):
    positional_error = "Multiple TEXT words. Quote the text or use --all - with stdin."


class SearchCommand(PropertyCommand):
    positional_error = "Multiple QUERY words. Quote the query."
    time_options = ("--since", "--until")

    def parse_args(self, ctx, args):
        return super().parse_args(ctx, self._join_date_time(list(args)))

    def _join_date_time(self, args):
        """Fold an unquoted ``DATE TIME`` value into one token.

        ``--until 2026-10-26 21:11:00 query`` reaches us as two words after the
        shell splits it; without this the clock part is read as a second query word.
        """
        joined = []
        index = 0
        while index < len(args):
            if args[index] == "--":
                joined.extend(args[index:])
                break
            option, equal, value = args[index].partition("=")
            if option not in self.time_options:
                joined.append(args[index])
            else:
                if not equal:
                    joined.append(option)
                    if index + 1 >= len(args):
                        break
                    index += 1
                    value = args[index]
                if (
                    _DATE.fullmatch(value)
                    and index + 1 < len(args)
                    and _CLOCK.fullmatch(args[index + 1])
                ):
                    index += 1
                    value = f"{value} {args[index]}"
                joined.append(f"{option}={value}" if equal else value)
            index += 1
        return joined
