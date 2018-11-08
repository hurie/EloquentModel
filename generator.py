"""
Created on 4/20/2016

@author: Azhar
"""
import logging
import os
import re
import sys
from collections import defaultdict, OrderedDict
from configparser import ConfigParser
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from mysql.connector import connection

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(process)d:%(name)s - %(levelname)s - %(message)s')
_log = logging.getLogger(__name__)

namespace_mark = '### Additional namespace #'
function_mark = '### User defined function #'

default_type = 'mixed'
datetime_type = '\Carbon\Carbon'
string_type = 'string'
integer_type = 'integer'
boolean_type = 'boolean'
float_type = 'float'

column_type = {
    'created_at': datetime_type,
    'updated_at': datetime_type,
    'deleted_at': datetime_type,
}

type_map = {
    'bigint': integer_type,
    'bit': integer_type,
    'bool': boolean_type,
    'boolean': boolean_type,
    'char': string_type,
    'date': datetime_type,
    'datetime': datetime_type,
    'decimal': float_type,
    'double': float_type,
    'enum': string_type,
    'float': float_type,
    'int': integer_type,
    'longtext': string_type,
    'mediumint': integer_type,
    'mediumtext': string_type,
    'numeric': float_type,
    'real': float_type,
    'set': string_type,
    'smallint': integer_type,
    'text': string_type,
    'time': datetime_type,
    'timestamp': datetime_type,
    'tinyint': integer_type,
    'tinytext': string_type,
    'varchar': string_type,
    'year': integer_type,
}

title_trans = ''.join(chr(c) if chr(c).isalnum() else '_' for c in range(256))


@lru_cache(maxsize=128)
def camelize(text):
    text = text.lower()
    return str(text[0].upper() +
               re.sub(r'_([a-z0-9])', lambda m: m.group(1).upper(), text[1:]))


@lru_cache(maxsize=128)
def plural(noun):
    rules = (
        ('[ml]ouse$', '([ml])ouse$', '\\1ice'),
        ('child$', 'child$', 'children'),
        ('booth$', 'booth$', 'booths'),
        ('foot$', 'foot$', 'feet'),
        ('ooth$', 'ooth$', 'eeth'),
        ('l[eo]af$', 'l([eo])af$', 'l\\1aves'),
        ('sis$', 'sis$', 'ses'),
        ('man$', 'man$', 'men'),
        ('ife$', 'ife$', 'ives'),
        ('eau$', 'eau$', 'eaux'),
        ('lf$', 'lf$', 'lves'),
        ('[sxz]$', '$', 'es'),
        ('[^aeioudgkprt]h$', '$', 'es'),
        ('(qu|[^aeiou])y$', 'y$', 'ies'),
        ('$', '$', 's')
    )

    def regexs():
        for rule in rules:
            pattern, search, replace = rule
            yield lambda word: re.search(pattern, word) and re.sub(search, replace, word)

    for regex in regexs():
        result = regex(noun)
        if result:
            return result


def table_definition(cnx):
    tables = defaultdict(lambda: {
        'class': None,
        'key': 'id',
        'autoincrement': 'false',
        'timestamps': 'false',
        'column': OrderedDict(),
        'fillable': [],
        'date': [],
        'null': [],
        'parent': OrderedDict(),
        'child': OrderedDict(),
    })

    with closing(cnx.cursor()) as cursor:
        cursor.execute('''\
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_KEY, IS_NULLABLE, DATA_TYPE, EXTRA
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
''')

        for table, column, key, null, col_type, extra in cursor:
            properties = tables[table]
            properties['name'] = camelize(table)

            if key == 'PRI':
                properties['key'] = column

            if 'auto_increment' in extra:
                properties['autoincrement'] = 'true'

            if null == 'YES':
                properties['null'].append(column)

            if column not in ['id', 'created_at', 'updated_at', 'deleted_at']:
                properties['fillable'].append(column)
            elif column in ['created_at', 'updated_at']:
                properties['timestamps'] = 'true'

            type_ = column_type.get(column)
            if type_ is None:
                type_ = type_map.get(col_type, default_type)
                if type_ == datetime_type:
                    properties['date'].append(column)
            elif column in ['created_at', 'updated_at', 'deleted_at']:
                properties['date'].append(column)

            properties['column'][column] = type_

    return tables


def load_relation(cnx, tables):
    with closing(cnx.cursor()) as cursor:
        cursor.execute('''\
SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE()
  AND REFERENCED_TABLE_SCHEMA = DATABASE()
  AND REFERENCED_TABLE_NAME IS NOT NULL
''')

        for table, column, ref_table, ref_column in cursor:
            if ref_table not in tables[table]['parent']:
                tables[table]['parent'][ref_table] = OrderedDict()

            if table not in tables[ref_table]['child']:
                tables[ref_table]['child'][table] = OrderedDict()

            tables[table]['parent'][ref_table][column] = ref_column
            tables[ref_table]['child'][table][column] = ref_column


def load_const(cnx, table, keys, value):
    key = ', '.join(keys)
    with closing(cnx.cursor()) as cursor:
        cursor.execute('''\
SELECT {key}, {value}
FROM {table}
ORDER BY {value}
'''.format(key=key, value=value, table=table))

        max_length = 0

        fields = {}
        for r in cursor:
            for c in r[:-1]:
                if isinstance(c, str) and c != '':
                    k = c
                    break
            else:
                continue

            v = r[-1]

            k = k.upper().translate(title_trans)
            k0 = None
            while k0 != k:
                k0, k = k, k.replace('__', '_')

            fields[k] = v

            max_length = max(max_length, len(k))

        result = []
        for k in sorted(fields, key=lambda x: fields[x]):
            pad = ' ' * (max_length - len(k))
            result.append('const %s%s = %s;' % (k, pad, fields[k]))

        return result


def main(config=None):
    local = Path(os.path.realpath(os.path.dirname(__file__)))

    # load configuration
    config = local / ('generator.ini' if config is None else config)
    if not config.exists():
        raise Exception('Unable to load configuration %s', config)

    conf = ConfigParser()
    conf.read_file(config.open())

    if not conf.has_section('options') and conf['options'].get('result_path') is None:
        raise Exception('result_path is undefined')

    path_model = Path(conf['options']['result_path'])

    if not path_model.exists():
        path_model.mkdir(parents=True)

    elif not path_model.is_dir():
        raise Exception('Unable to use "%s" as path_model', path_model)

    db = conf['db']

    namespace = conf['options'].get('namespace', 'App')
    ignore = [x for x in map(str.strip, conf['options'].get('ignored_table', []).splitlines()) if x]
    hidden_column = [x for x in map(str.strip, conf['options'].get('hidden_column', []).splitlines()) if x]
    history_suffix = conf['options'].get('history_table_suffix')

    base_class = base_namespace = conf.get('options', 'base_class', fallback='Eloquent')
    if ' as ' in base_class:
        base_class, base_namespace = base_class.split(' as ')
    else:
        base_class = base_class.split('\\')[-1]

    base_class.strip()
    base_namespace.strip()

    base_classes = {}
    if conf.has_section('base'):
        for name, value in conf.items('base'):
            if ' as ' in value:
                base = value.split(' as ')[1].strip()
            else:
                base = value.split('\\')[-1].strip()

            base_classes[name] = (base, value)

    if 'constant' in conf:
        const_fields = [x for x in map(str.strip, conf['constant']['default_value_column'].splitlines()) if x]
        extract_const = conf['constant/key_column']
        extract_field = conf['constant/value_column']
    else:
        const_fields = []
        extract_const = {}
        extract_field = {}

    path_ref = conf['options'].get('reference_path')
    path_template = local / 'template'

    _log.info('cleanup %s', path_model)
    for f in path_model.iterdir():
        if f.is_file():
            f.unlink()

    template_history = (path_template / 'history.txt').read_text()
    template_one_to_one = (path_template / 'one_to_one.txt').read_text()
    template_one_to_many = (path_template / 'one_to_many.txt').read_text()
    template_many_to_one = (path_template / 'many_to_one.txt').read_text()
    template_model = (path_template / 'model.txt').read_text()

    table_consts = {}

    # open connection to database then load table definition, load tabel relation and
    # value constant if specified
    _log.info('connection')
    with closing(connection.MySQLConnection(**db)) as cnx:
        _log.info('loading table definition')
        tables = table_definition(cnx)

        _log.info('loading table relation')
        load_relation(cnx, tables)

        for table, value in extract_const.items():
            if table in tables:
                if table in extract_field:
                    keys = [extract_field[table]]
                else:
                    keys = const_fields
                table_consts[table] = load_const(cnx, table, keys, value)

    for table, properties in tables.items():
        if table in ignore:
            continue

        _log.info('processing table %s', table)

        key = properties['key']
        name = properties['name']

        use = [
            'use %s;' % base_namespace,
            'use Illuminate\Database\Eloquent\Collection;',
            'use Illuminate\Database\Query\Builder;',
        ]

        docs = []
        const = ''
        hidden = []
        methods = []
        fillable = ["        '%s'" % column for column in properties['fillable']]
        dates = []
        casts = []

        props = []
        wheres = []
        relations = []

        # add history related method if table history exists
        if history_suffix and (table + history_suffix) in tables:
            methods.append(template_history.format(
                table=table,
                key=key,
                model=name
            ))

        if table in table_consts and table_consts[table]:
            const = ''.join(['\n    ', '\n    '.join(table_consts[table]), '\n'])

        column_length = 0
        type_length = 0

        for column in properties['column']:
            if column not in properties['date']:
                column_length = max(column_length, len(column))

        for column, col_type in properties['column'].items():
            prop_type = col_type
            method = camelize(column)

            if column in properties['null']:
                prop_type = 'null|' + prop_type
            type_length = max(type_length, len(prop_type))

            if column in hidden_column:
                hidden.append("        '%s'" % column)

            props.append((prop_type, column))

            wheres.append('@method static Builder|%s where%s($value)' % (name, method))

            if column in properties['date']:
                dates.append("        '%s'" % column)
            else:
                casts.append("        '%s'%s => '%s'" % (column, ' ' * (column_length - len(column)), col_type))

        # relation
        for ref_table, columns in properties['child'].items():
            ref_key = tables[ref_table]['key']
            ref_name = tables[ref_table]['name']

            for column, ref_column in columns.items():
                if column == ref_key:
                    ref = ref_name[0].lower() + ref_name[1:]
                    type_length = max(type_length, len(ref_name) + 5)

                    relations.append((ref_name, ref))
                    methods.append(template_one_to_one.format(
                        ref=ref,
                        namespace=namespace,
                        model=ref_name,
                        column=column,
                        ref_column=ref_column
                    ))
                    use.append('use Illuminate\Database\Eloquent\Relations\HasOne;')

                else:
                    if column.startswith(key):
                        prefix = column.replace(key, '')
                        ref = camelize(ref_table + prefix)
                    else:
                        ref = ref_name

                    ref = plural(ref[0].lower() + ref[1:])
                    type_length = max(type_length, len(ref_name) + 13 + 5)

                    relations.append(('Collection|%s[]' % ref_name, ref))
                    methods.append(template_one_to_many.format(
                        ref=ref,
                        namespace=namespace,
                        model=ref_name,
                        column=column,
                        ref_column=ref_column
                    ))
                    use.append('use Illuminate\Database\Eloquent\Relations\HasMany;')

        for ref_table, columns in properties['parent'].items():
            ref_key = tables[ref_table]['key']
            ref_name = tables[ref_table]['name']

            for column, ref_column in columns.items():
                if column.startswith(ref_key):
                    prefix = column.replace(ref_key, '')
                    ref = camelize(ref_table + prefix)
                else:
                    ref = ref_name

                ref = ref[0].lower() + ref[1:]
                type_length = max(type_length, len(ref_name) + 5)

                relations.append((ref_name, ref))
                methods.append(template_many_to_one.format(
                    ref=ref,
                    namespace=namespace,
                    model=ref_name,
                    column=column,
                    ref_column=ref_column
                ))
                use.append('use Illuminate\Database\Eloquent\Relations\BelongsTo;')

        if props:
            props = ['@property %s%s $%s' % (prop_type, ' ' * (type_length - len(prop_type)), column) for
                     prop_type, column in props]
            docs.append('\n * '.join(props))

        if relations:
            relations = ['@property-read %s%s $%s' % (ref_name, ' ' * (type_length - len(ref_name) - 5), ref) for
                         ref_name, ref in relations]
            docs.append('\n * '.join(relations))

        if wheres:
            docs.append('\n * '.join(wheres))

        if table in base_classes:
            base, cls = base_classes[table]
            use.append('use %s;' % cls)
            use.remove('use %s;' % base_namespace)
        else:
            base = base_class

        if 'deleted_at' in properties['column']:
            use.append('use Illuminate\Database\Eloquent\SoftDeletes;')

        use = '\n'.join(sorted(set(use)))
        docs = '\n *\n * '.join(docs)
        fillable = ',\n'.join(fillable)
        dates = ',\n'.join(dates)
        hidden = ',\n'.join(hidden)
        casts = ',\n'.join(casts)
        methods = ''.join(methods)

        if use:
            use += '\n'

        if docs:
            docs = '\n * %s\n *' % docs

        if fillable:
            fillable = '\n%s,\n    ' % fillable

        if dates:
            dates = '\n%s,\n    ' % dates

        if hidden:
            hidden = '\n%s,\n    ' % hidden

        if casts:
            casts = '\n%s,\n    ' % casts

        f = None if path_ref is None else (Path(path_ref) / (name + '.php'))
        if f is not None and f.exists():
            is_namespace = False
            is_trait = False
            is_function = False

            regions = []
            additional_ns = []
            additional_function = []

            old_texts = f.read_text().splitlines()
            for line in old_texts:
                line_stripped = line.strip()

                if line.startswith('class') and 'extends' in line_stripped:
                    is_trait = True
                    lines = line_stripped.split(' ')
                    if len(lines) > 4:
                        base += ' %s' % ' '.join(lines[4:])

                elif line_stripped.startswith('//region'):
                    region = line_stripped.replace('//region', '').strip()
                    regions.append(region)

                    if region == namespace_mark:
                        is_namespace = True
                    elif region == function_mark:
                        is_function = True
                    elif is_namespace:
                        additional_ns.append(line)
                    elif is_function:
                        additional_function.append(line)

                elif line_stripped.startswith('//endregion'):
                    region = regions.pop(-1)

                    if region == namespace_mark:
                        is_namespace = False
                    elif region == function_mark:
                        is_function = False
                    elif is_namespace:
                        additional_ns.append(line)
                    elif is_function:
                        additional_function.append(line)

                elif is_trait:
                    if line_stripped.startswith('use'):
                        const = '\n%s\n%s' % (line, const)
                    elif not line_stripped:
                        is_trait = False

                elif is_namespace:
                    additional_ns.append(line)

                elif is_function:
                    additional_function.append(line)

            additional_ns = '\n'.join(additional_ns)
            if additional_ns:
                use = '%s\n//region %s\n%s\n//endregion\n' % (use, namespace_mark, additional_ns)

            additional_function = '\n'.join(additional_function)
            if additional_function:
                methods = '%s\n    //region %s\n%s\n    //endregion\n' % (methods, function_mark, additional_function)

        if 'deleted_at' in properties['column'] and 'SoftDeletes' not in const:
            if const:
                const = '    use SoftDeletes;\n%s' % const
            else:
                const = '    use SoftDeletes;'

        text = template_model.format(
            namespace=namespace,
            use=use,
            name=name,
            const=const,
            docs=docs,
            base=base,
            table=table,
            key=key,
            incrementing=properties['autoincrement'],
            timestamps=properties['timestamps'],
            hidden=hidden,
            fillable=fillable,
            dates=dates,
            casts=casts,
            methods=methods
        )

        f = path_model / (name + '.php')
        with f.open(mode='w', newline='\n') as f:
            f.write(text)

    _log.info('done')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else None)
