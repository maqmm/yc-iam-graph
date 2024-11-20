import asyncio
import aiohttp
import re
import yaml
import json
import os
import shutil
from pathlib import Path

# URLs
ROLES_REFERENCE_URL = 'https://raw.githubusercontent.com/yandex-cloud/docs/refs/heads/master/ru/iam/roles-reference.md'
PRESETS_YAML_URL = 'https://raw.githubusercontent.com/yandex-cloud/docs/refs/heads/master/ru/presets.yaml'
ROLES_PRIMITIVE = 'https://raw.githubusercontent.com/yandex-cloud/docs/refs/heads/master/ru/_includes/roles-primitive.md'

async def download_content(session, url):
    async with session.get(url) as response:
        response.raise_for_status()
        #print('downloaded', url)
        return await response.text()

def load_presets_yaml(yaml_content):
    yaml_dict = yaml.safe_load(yaml_content)
    return yaml_dict

def replace_variables(line, variables):
    pattern = r'{{\s*([^}]+)\s*}}'
    matches = re.findall(pattern, line)
    for var_name in matches:

        # Преобразуем имя переменной в путь для YAML
        # Например: roles_metastore_auditor -> roles.metastore.auditor
        path = 'default.'+var_name.strip()

        # Получаем значение по пути
        keys = path.split('.')
        current = variables
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                current = None
                break

        # Заменяем переменную на значение
        if current is not None:
            template = "{{ " + var_name.strip() + " }}"
            line = line.replace(template, str(current))
    return line

def parse_markdown(markdown_content, variables):
    #print(markdown_content)
    lines = markdown_content.split('\n')
    hierarchy = []
    roles_tree = {}
    current_section = None
    current_description = []
    processing_description = False
    last_header = None

    def add_to_tree(tree, path_parts, role_data):
        """Рекурсивно добавляет роль в дерево"""
        if not path_parts:
            return role_data
        current = path_parts[0]
        remaining = path_parts[1:]
        
        if current not in tree:
            tree[current] = {}
        
        if not remaining:
            if isinstance(tree[current], dict):
                tree[current].update(role_data)
            else:
                tree[current] = role_data
        else:
            tree[current] = add_to_tree(tree[current], remaining, role_data)
        return tree

    def update_role_description(tree, role_name, description):
        """Обновляет описание роли в дереве"""
        def find_and_update(t):
            for k, v in t.items():
                if k == role_name and isinstance(v, dict):
                    v['description'] = description
                    return True
                elif isinstance(v, dict):
                    if find_and_update(v):
                        return True
            return False
        find_and_update(tree)

    current_role = None
    
    for line in lines:
        line = replace_variables(line, variables)

        # Match headers to build hierarchy
        header_match = re.match(r'^(#{1,6})\s+(.*?)(?:\s+\{#(.*?)\})?$', line)
        if header_match:
            if current_role and current_description:
                description = ' '.join(current_description).strip()
                if description:
                    update_role_description(roles_tree, current_role, description)
                current_description = []

            level = len(header_match.group(1))
            title = header_match.group(2).strip()
            slug = header_match.group(3) if header_match.group(3) else title.lower().replace(' ', '-').replace('.', '-')
            
            while hierarchy and hierarchy[-1][0] >= level:
                hierarchy.pop()
            hierarchy.append((level, title, slug))
            last_header = title  # Сохраняем последний заголовок
            processing_description = False
            continue

        # Match include statements to get roles
        include_match = re.match(r'^\{%\s+include\s+\[(.*?)\]\((.*?)\)\s+%}$', line)
        if include_match:
            if current_role and current_description:
                description = ' '.join(current_description).strip()
                if description:
                    update_role_description(roles_tree, current_role, description)
                current_description = []

            role_name = last_header  # Используем последний заголовок как имя роли
            role_path = include_match.group(2).replace('../', '')
            current_role = role_name
            
            # Создаем путь для добавления в дерево, исключая последний заголовок
            path_parts = [h[1] for h in hierarchy[:-1]] + [role_name]
            role_data = {
                'description': '',
                'path': role_path
            }
            
            # Добавляем в дерево
            roles_tree = add_to_tree(roles_tree, path_parts, role_data)
            processing_description = True
            continue

        if processing_description and line.strip():
            current_description.append(line.strip())

    # Сохраняем последнее описание
    if current_role and current_description:
        description = ' '.join(current_description).strip()
        if description:
            update_role_description(roles_tree, current_role, description)

    return roles_tree

async def fetch_role_descriptions(roles_tree, variables, session):
    base_url = 'https://raw.githubusercontent.com/yandex-cloud/docs/refs/heads/master/ru/'

    async def fetch_description(value):
        role_url = base_url + value['path']
        try:
            content = await download_content(session, role_url)
            # Replace variables in content
            content = replace_variables(content, variables)
            # Extract description (assuming it's the first non-empty paragraph)
            paragraphs = [p.strip() for p in content.strip().split('\n\n') if p.strip()]
            description = paragraphs[0] if paragraphs else 'Описание не найдено.'
            # Clean markdown formatting
            description = re.sub(r'(.*?)', r'\1', description)
            description = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', description)
            value['description'] = description
        except Exception as e:
            value['description'] = 'Описание не найдено.'

    async def recurse(tree):
        tasks = []
        for key, value in tree.items():
            if isinstance(value, dict):
                if 'path' in value:
                    # Schedule fetching role description
                    tasks.append(fetch_description(value))
                else:
                    # Schedule recursion asynchronously
                    tasks.append(recurse(value))
        if tasks:
            await asyncio.gather(*tasks)

    await recurse(roles_tree)


def generate_mermaid_mindmap(roles_tree):
    graph_lines = ['mindmap']

    def escape_label(label):
        # Escape special characters and replace dots with underscores for node IDs
        return label.replace('"', '\\"').replace('\n', ' ').replace('\\', '\\\\').replace('`', "'")

    def escape_node_id(node_id):
        # Replace dots with underscores for node IDs
        return node_id.replace('.', '_')

    def recurse(tree, indent=0):
        for key, value in tree.items():
            indent_str = '  ' * indent
            label = escape_label(key)
            node_id = escape_node_id(key)
            
            if 'description' in value:
                # Добавляем круглый узел с названием роли
                graph_lines.append(f'{indent_str}{node_id}("`{label}`")')
                # Добавляем описание
                if value['description']:
                    desc_label = escape_label(value['description'])
                    graph_lines.append(f'{indent_str}  {node_id}_desc["`{desc_label}`"]')
            else:
                # Это секция
                graph_lines.append(f'{indent_str}{label}')
                # Рекурсивно обрабатываем дочерние элементы
                recurse(value, indent + 1)

    recurse(roles_tree)
    return '\n'.join(graph_lines)

def create_obsidian_vault(json_data, output_dir):
    # def create_markdown_file(path, content, parent=None, children=None):
    #     # Создаем директории, если они не существуют
    #     os.makedirs(os.path.dirname(os.path.join(output_dir, path)), exist_ok=True)
        
    #     with open(os.path.join(output_dir, path), 'w', encoding='utf-8') as f:
    #         # Добавляем заголовок
    #         title = os.path.splitext(os.path.basename(path))[0]
    #         f.write(f"# {title}\n\n")
            
    #         # Добавляем описание, если оно есть
    #         if "description" in content:
    #             f.write(f"{content['description']}\n\n")
            
    #         # Добавляем связи
    #         links = []
    #         if parent:
    #             links.append(parent)
    #         if children:
    #             links.extend(children)
                
    #         if links:
    #             f.write("\n## Связи\n")
    #             for link in links:
    #                 f.write(f"- [[{link}]]\n")

    def generate_paths(role_name, service_name, root='roles'):
        paths = []
        parts = role_name.split('.')
        
        # Создаем список для хранения путей категорий и роли
        category_paths = []
        
        # Генерируем пути для категорий
        for i in range(1, len(parts)):
            current_name = '.'.join(parts[:i])
            path_parts = ['.'.join(parts[:j]) for j in range(1, i)]
            role_path = '/'.join(path_parts) if path_parts else ''
            full_path = f"_categories/{role_path}/{current_name}.md" if role_path else f"_categories/{current_name}.md"
            category_paths.append((current_name, full_path))
        
        # Генерируем путь для самой роли
        i = len(parts)
        current_name = '.'.join(parts[:i])
        path_parts = ['.'.join(parts[:j]) for j in range(1, i)]
        role_path = '/'.join(path_parts) if path_parts else ''
        role_full_path = f"_roles/{role_path}/{current_name}.md" if role_path else f"_roles/{current_name}.md"
        return category_paths, role_full_path


    def create_markdown_file(path, content, parent=None, children=None):
        # Создаем директории, если они не существуют
        os.makedirs(os.path.dirname(os.path.join(output_dir, path)), exist_ok=True)
        
        with open(os.path.join(output_dir, path), 'w', encoding='utf-8') as f:
            # Добавляем заголовок
            title = os.path.dirname(path).split('/', 1)[0]

            title = 'Роль' if title == '_roles' else 'Категория'
            f.write(f"# {title}\n\n")
            
            # Добавляем описание, если оно есть
            if "description" in content:
                f.write(f"{content['description']}\n\n")
            
            # Добавляем связи
            if parent:
                f.write("\n#### Родители\n\n")
                f.write(f"- [[{parent}]]\n")
            if children:
                f.write("\n#### Дети\n")
                for child in children:
                    f.write(f"- [[{child}]]\n")


    def process_json(data, parent_name=None):
        # Словарь для хранения детей каждой категории
        category_children = {}
        
        def collect_direct_children(prefix):
            """Собирает всех прямых детей для заданного префикса"""
            direct_children = set()
            prefix_parts = len(prefix.split('.')) if prefix else 0
            
            for key in data.keys():
                key_parts = key.split('.')
                # Если ключ начинается с префикса и имеет только на одну часть больше
                if (prefix and key.startswith(prefix + '.') and len(key_parts) == prefix_parts + 1) or \
                   (not prefix and len(key_parts) == 1):
                    direct_children.add(key)
            return direct_children

        # Первый проход: собираем информацию о детях
        for key, value in data.items():
            if isinstance(value, dict) and "description" in value:
                category_paths, role_path = generate_paths(key, parent_name)
                
                # Собираем детей для каждой категории
                for i, (cat_name, _) in enumerate(category_paths):
                    if cat_name not in category_children:
                        # Собираем всех прямых детей для текущей категории
                        if cat_name == 'Примитивные роли':
                            category_children['ROLES'] = collect_direct_children(cat_name)
                        else:
                            category_children[cat_name] = collect_direct_children(cat_name)
        
        # Второй проход: создаем файлы
        for key, value in data.items():
            if isinstance(value, dict):
                if "description" in value:
                    category_paths, role_path = generate_paths(key, parent_name)
                    value["path"] = role_path
                    
                    # Создаем файлы категорий
                    for i, (cat_name, cat_path) in enumerate(category_paths):
                        parent = category_paths[i-1][0] if i > 0 else parent_name
                        children = list(category_children.get(cat_name, set()))
                        
                        create_markdown_file(
                            cat_path,
                            {"description": f"{cat_name}"},
                            parent=None,
                            children=None
                        )
                    
                    # Создаем файл роли
                    parent = category_paths[-1][0] if category_paths else 'ROLES'
                    #print(role_path, parent, category_paths)
                    create_markdown_file(
                        role_path,
                        value,
                        parent=parent
                    )
                
                process_json(value, key)

    def create_root_category(output_dir):
        """Создает или обновляет корневую категорию ROLES.md"""
        categories_dir = Path(output_dir) / "_categories"
        root_file = categories_dir / "ROLES.md"
        os.makedirs(os.path.dirname(root_file), exist_ok=True)

        content = "# ROLES\n\n"
        content += "Корневая категория для всех ролей\n\n"

        with open(root_file, 'w', encoding='utf-8') as f:
            f.write(content)

    # Создаем основную директорию vault
    shutil.rmtree(output_dir, ignore_errors=True)
    os.makedirs(output_dir, exist_ok=True)

    create_root_category(output_dir) #ради фикса анимации графа

    # Начинаем обработку JSON
    process_json(json_data)


def update_categories_links(vault_dir):
    """
    Обновляет связи в файлах категорий на основе структуры каталогов
    
    Args:
        vault_dir: путь к корневой директории хранилища
    """

    categories_dir = Path(vault_dir) / "_categories"
    roles_dir = Path(vault_dir) / "_roles"

    def get_root_children():
        """Получает список категорий первого уровня"""
        root_categories = set()
        for file_path in categories_dir.glob("*.md"):
            if file_path.name != "ROLES.md":
                root_categories.add(file_path.stem)
        return sorted(root_categories)

    def create_root_category():
        """Создает или обновляет корневую категорию ROLES.md"""
        root_file = categories_dir / "ROLES.md"
        subcategories, roles = get_direct_children(root_file)
        
        content = "# ROLES\n\n"
        content += "Корневая категория для всех ролей\n\n"
        
        if subcategories or roles:
            content += "\n#### Дети\n"

        if subcategories:
            content += "\n###### Подкатегории\n"
            for subcategory in subcategories:
                content += f"- [[{subcategory}]]\n"

        if roles:
            content += "\n###### Роли\n"
            for role in roles:
                content += f"- [[{role}]]\n"
        
        with open(root_file, 'w', encoding='utf-8') as f:
            f.write(content)

    def get_parent_path(file_path):
        """Получает путь родительской категории"""
        # Убираем _categories из пути
        relative_path = file_path.relative_to(categories_dir)

        # Если файл находится в корне _categories
        if len(relative_path.parts) == 1:
            return "ROLES"

        # Убираем имя файла и берем родительский путь
        parent_parts = relative_path.parts[:-1]
        if not parent_parts:
            return "ROLES"
            
        # Формируем имя родительского файла
        parent_name = '.'.join(str(part) for part in parent_parts)

        return f"{parent_name}"


    def get_direct_children(category_path):
        """Получает списки прямых детей-категорий и детей-ролей"""

        subcategories = set()
        roles = set()
        
        # Получаем базовый путь категории без расширения
        base_path = str(category_path.relative_to(categories_dir))
        
        # Обработка имени категории
        category_name = category_path.stem
        if category_name == 'ROLES':
            # Для корневого элемента ROLES
            # Ищем категории-потомки первого уровня
            for child_category in categories_dir.glob("*.md"):
                child_name = child_category.stem
                if child_name != 'ROLES':  # Исключаем сам файл ROLES.md
                    subcategories.add(child_name)

            # Ищем роли первого уровня
            for role_file in roles_dir.glob("*.md"):
                role_name = role_file.stem
                if '.' not in role_name:  # Берем только роли первого уровня
                    roles.add(role_name)
        else:
            # Для остальных категорий
            base_path = base_path.replace('.md', '')
            
            # Ищем категории-потомки
            for child_category in categories_dir.rglob("*.md"):
                child_name = child_category.stem
                if child_name.startswith(f"{category_name}.") and \
                   len(child_name.split('.')) == len(category_name.split('.')) + 1:
                    subcategories.add(child_name)

            # Ищем роли-потомки
            corresponding_roles_path = roles_dir / base_path
            if corresponding_roles_path.exists() and corresponding_roles_path.is_dir():
                for role_file in corresponding_roles_path.rglob("*.md"):
                    role_name = role_file.stem
                    if role_name.startswith(f"{category_name}.") and \
                       len(role_name.split('.')) == len(category_name.split('.')) + 1:
                        roles.add(role_name)

        return sorted(subcategories), sorted(roles)

    def update_category_file(file_path):
        """Обновляет содержимое файла категории"""
        #print(f"Обработка файла: {file_path}")
        
        # Читаем текущее содержимое файла
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Находим заголовок и описание
        header = []
        for line in lines:
            if '####' in line:
                break
            header.append(line)

        # Получаем родителя и детей
        parent = get_parent_path(file_path)
        
        subcategories, roles = get_direct_children(file_path)

        # Формируем новое содержимое
        new_content = ''.join(header)
        
        if parent:
            new_content += "\n#### Родители\n\n"
            new_content += f"- [[{parent}]]\n"
        
        if subcategories or roles:
            new_content += "\n\n#### Дети\n\n"

        if subcategories:
            new_content += "###### Подкатегории\n"
            for subcategory in subcategories:
                new_content += f"- [[{subcategory}]]\n"

        if roles:
            new_content += "###### Роли\n"
            for role in roles:
                new_content += f"- [[{role}]]\n"

        # Записываем обновленное содержимое
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
    
    # Создаем корневую категорию
    create_root_category()
    # Обрабатываем все файлы категорий
    for category_file in categories_dir.rglob("*.md"):
        if category_file.name != "ROLES.md":  # Пропускаем корневой файл
            update_category_file(category_file)


def set_random_colors_for_services(vault_dir):
    """
    Устанавливает случайные цвета для сервисов в графе Obsidian
    путем создания/обновления файла graph.json
    
    Args:
        vault_dir: путь к корневой директории хранилища
    """
    import json
    import random
    from pathlib import Path

    colors = [
        "#FF0000",  # Красный
        "#00FF00",  # Зеленый
        "#0000FF",  # Синий
        "#FFFF00",  # Желтый
        "#FF00FF",  # Магента
        "#00FFFF",  # Циан
        "#800000",  # Бордовый
        "#808000",  # Оливковый
        "#008000",  # Темно-зеленый
        "#800080",  # Фиолетовый
        "#008080",  # Темно-бирюзовый
        "#000080",  # Темно-синий
        "#FFA500",  # Оранжевый
        "#FFC0CB",  # Розовый
        "#A52A2A",  # Коричневый
        "#D2691E",  # Шоколадный
        "#FF7F50",  # Коралловый
        "#9ACD32",  # Желто-зеленый
        "#7FFF00",  # Ярко-зеленый
        "#6495ED",  # Васильковый
        "#40E0D0",  # Бирюзовый
        "#FF1493",  # Глубокий розовый
        "#00CED1",  # Темно-бирюзовый
        "#FF69B4",  # Ярко-розовый
        "#CD5C5C",  # Индийский красный
        "#4B0082",  # Индиго
        "#20B2AA",  # Светлый морской зеленый
        "#87CEFA",  # Светло-голубой
        "#778899",  # Светло-серый
        "#B0C4DE",  # Светло-стальной синий
        "#32CD32",  # Лаймовый
        "#66CDAA",  # Средний аквамарин
        "#BA55D3",  # Средняя орхидея
        "#9370DB",  # Средний пурпурный
        "#3CB371",  # Средне-морской зеленый
        "#7B68EE",  # Средний синий шифер
        "#00FA9A",  # Средне-весенний зеленый
        "#C71585",  # Красный фуксии
        "#191970",  # Темно-ночной синий
        "#808080",  # Серый
        "#000000",  # Черный
        "#FFFFFF",  # Белый
        "#FFD700",  # Золотой
        "#DC143C",  # Малиновый
        "#B8860B",  # Темно-золотисто-родниковый
        "#006400",  # Темно-зеленый
        "#B22222",  # Огненно-кирпичный
        "#2E8B57",  # Морской зеленый
        "#8B0000",  # Темно-красный
        "#FF4500",  # Оранжево-красный
        "#DA70D6",  # Бледная фуксия
        "#ADFF2F",  # Зеленовато-желтый
        "#F5DEB3",  # Пшеничный
        "#5F9EA0",  # Кадетский синий
        "#D2B48C",  # Загар
        "#FA8072",  # Лососевый
        "#E9967A",  # Темно-лососевый
        "#FF6347",  # Томато
        "#FFDEAD",  # Навахо белый
        "#FAFAD2",  # Светлый золотистый
        "#7FFFD4",  # Аквамарин
        "#FFE4C4",  # Бисквит
        "#8A2BE2",  # Синевато-фиолетовый
        "#A52A2A",  # Коричневый
        "#DEB887",  # Палисандр
        "#5F9EA0",  # Кадетский голубой
        "#7FFF00",  # Шартрез
        "#D2691E",  # Каштановый
        "#FF7F50",  # Коралловый
        "#6495ED",  # Васильковый
        "#FFF8DC",  # Бледно-циан
        "#DC143C",  # Малиновый
        "#00FFFF",  # Аква
        "#008B8B",  # Темный бирюзовый
        "#B8860B",  # Темный золотистый
        "#A9A9A9",  # Темно-серый
        "#006400",  # Темный зеленый
        "#BDB76B",  # Темный хаки
        "#8B008B",  # Темный магента
        "#556B2F",  # Темный оливковый зеленый
        "#FF8C00",  # Темно-оранжевый
        "#9932CC",  # Темная орхидея
        "#8B0000",  # Темная красная
        "#8B4513",  # Седло коричневый
        "#2F4F4F",  # Темно сливовый
        "#9400D3",  # Темный фиолетовый
        "#FF1493",  # Глубокий розовый
        "#00BFFF",  # Стальной синий
        "#696969",  # Тускло-серый
        "#1E90FF",  # Доджер синий
        "#B22222",  # Огненно-брик
        "#FFFAF0",  # Цвет слоновой кости
        "#228B22",  # Лесной зеленый
        "#FF00FF",  # Фуксия
    ]

    def get_services():
        """Получает список всех уникальных сервисов из имен файлов"""
        categories_dir = Path(vault_dir) / "_categories"
        services = set()
        
        for file_path in categories_dir.rglob("*.md"):
            if file_path.name != "ROLES.md":
                # Берем первую часть имени файла (до первой точки)
                service = file_path.stem.split('.')[0]
                services.add(service)
        
        return sorted(list(services))

    def create_graph_config(services):
        """Создает конфигурацию графа с цветами для сервисов"""
        # Перемешиваем цвета для случайного выбора
        available_colors = colors.copy()
        random.shuffle(available_colors)
        
        # Создаем базовую структуру конфигурации
        config = {
            "collapse-filter": True,
            "search": "",
            "showTags": False,
            "showAttachments": False,
            "hideUnresolved": False,
            "showOrphans": True,
            "collapse-color-groups": True,
            "colorGroups": [],
            "collapse-display": True,
            "showArrow": False,
            "textFadeMultiplier": 0,
            "nodeSizeMultiplier": 1,
            "lineSizeMultiplier": 1,
            "collapse-forces": True,
            "centerStrength": 0.518713248970312,
            "repelStrength": 10,
            "linkStrength": 1,
            "linkDistance": 250,
            "scale": 1,
            "close": False
        }
        
        # Добавляем группы цветов для каждого сервиса
        for i, service in enumerate(services):
            color = available_colors[i % len(available_colors)]
            
            # Группа для категорий
            categories_group = {
                "query": f"path:_categories/{service}",
                "color": {
                    "a": 1,
                    "rgb": color[1:]  # Убираем # из начала цвета
                }
            }
            config["colorGroups"].append(categories_group)
            
            # Группа для ролей
            roles_group = {
                "query": f"path:_roles/{service}",
                "color": {
                    "a": 1,
                    "rgb": color[1:]  # Тот же цвет для ролей
                }
            }
            config["colorGroups"].append(roles_group)

        # Добавляем специальную группу для корневого файла ROLES
        root_group = {
            "query": "file:ROLES",
            "color": {
                "a": 1,
                "rgb": "FFFFFF"  # Белый цвет для корневого файла
            }
        }
        config["colorGroups"].append(root_group)
        
        return config

    # Получаем список сервисов
    services = get_services()
    
    # Создаем конфигурацию
    graph_config = create_graph_config(services)
    
    # Создаем директорию .obsidian если её нет
    config_dir = Path(vault_dir) / ".obsidian"
    config_dir.mkdir(parents=True, exist_ok=True)
    
    # Сохраняем конфигурацию
    with open(config_dir / "graph.json", 'w', encoding='utf-8') as f:
        json.dump(graph_config, f, indent=2)


async def main():
    async with aiohttp.ClientSession() as session:
        # Step 1: Download roles-reference.md and presets.yaml
        markdown_content, presets_yaml_content, primitive = await asyncio.gather(
            download_content(session, ROLES_REFERENCE_URL),
            download_content(session, PRESETS_YAML_URL),
            download_content(session, ROLES_PRIMITIVE),
        )
        variables = load_presets_yaml(presets_yaml_content)
        
        markdown_content = markdown_content.replace("{% include [roles-primitive](../_includes/roles-primitive.md) %}", primitive)

        # Step 2: Parse markdown to build roles tree
        roles_tree = parse_markdown(markdown_content, variables)

        # Step 3: Fetch role descriptions asynchronously
        await fetch_role_descriptions(roles_tree, variables, session)

        print("==\n==\n==\n", json.dumps(roles_tree, sort_keys=True, indent=4, ensure_ascii=False), "\n==\n==\n==")

        create_obsidian_vault(roles_tree, "yc-obs-roles")
        update_categories_links("yc-obs-roles")
        set_random_colors_for_services("yc-obs-roles")
        # Step 4: Generate Mermaid graph
        #mermaid_graph = generate_mermaid_mindmap(roles_tree)

        # Output the Mermaid graph
        #print("Generated Mermaid Graph:")
        #print(mermaid_graph)

        # Optionally, write to a file
        #with open('roles_graph.mmd', 'w', encoding='utf-8') as f:
            #f.write(mermaid_graph)
        #print("\nMermaid graph saved to roles_graph.mmd")

if __name__ == '__main__':
    asyncio.run(main())