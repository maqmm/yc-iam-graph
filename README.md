# Дерево ролей IAM Yandex Cloud в Obsidian

Скелет репозитория - скрипт, который парсит документацию *Yandex Cloud* и генерирует на её основе древовидную структуру *IAM* ролей в *Obsidian* с помощью wiki-links.

## Startup

`git clone https://github.com/maqmm/yc-iam-graph.git`

**Добавить `yc-obs-roles` как vault в Obsidian.**

Обновлять (при изменениях ролей *YC*) можно, запустив скрипт `main.py`.

`python3 main.py`

## Usage

Просмотр графа ролей.

![animate](/gifs/animate.gif)


Исследование ролей.

![explore](/gifs/explore.gif)


Фильтрация элементов на графе.

![filter](/gifs/filter.gif)


Настройка визуализации графа.

![viewgraph](/gifs/viewgraph.gif)
