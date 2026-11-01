"""Small server-side page navigation, retaining repeated genre query arguments."""
from flask import request, url_for


def paginate(total, size=36, anchor=''):
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    pages = max(1, (total+size-1)//size)
    page = min(page, pages)
    def link(number):
        args = request.args.to_dict(flat=False)
        args['page'] = number
        return url_for(request.endpoint, **(request.view_args or {}), **args, _anchor=anchor or None)
    window = sorted({1, pages, *range(max(1, page-2), min(pages, page+2)+1)})
    links, last = [], 0
    for number in window:
        if last and number > last+1:
            links.append(None)
        links.append(dict(number=number, url=link(number)))
        last = number
    return dict(page=page, pages=pages, total=total, offset=(page-1)*size, size=size, links=links,
                previous=link(page-1) if page>1 else None, next=link(page+1) if page<pages else None)
