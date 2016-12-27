import requests


class EdrApiClient(object):
    """Base class for making requests to EDR"""

    def __init__(self, server, token):
        self.token = token
        self.url = '{server}/1.0/subjects'.format(server=server)
        self.headers = {"Accept": "application/json", 'Authorization': 'Token {token}'.format(token=self.token)}

    def get_by_code(self, code):
        param = 'code' if code.isdigit() and len(code) < 13 else 'passport'  # find out we accept edrpou or passport code
        url = '{url}?{param}={code}'.format(url=self.url, param=param, code=code)
        response = requests.get(url=url, headers=self.headers)

        return response
