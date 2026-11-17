# API shapes

Field names and types only. No ticket text.

## GET /status

```
{'mode': 'str', 'candidate': 'str', 'endpoints': ['list of 8', 'str'], 'live_ends_at': 'null', 'open_tickets': 'int', 'handled_tickets': 'int'}
```

Values (mode and counts only): {'mode': 'practice', 'open_tickets': 30, 'handled_tickets': 0}

## GET /examples

```
{'examples': ['list of 10', {'id': 'str', 'body': 'str', 'label': {'action': 'str', 'reason': 'str', 'category': 'str', 'language': 'str', 'priority': 'str'}, 'channel': 'str', 'subject': 'str', 'received_at': 'str', 'customer_plan': 'str'}]}
```

Count: 10

## GET /tickets?limit=3&status=all

```
{'tickets': ['list of 3', {'id': 'str', 'body': 'str', 'channel': 'str', 'handled': 'null', 'subject': 'str', 'received_at': 'str', 'customer_plan': 'str'}], 'next_cursor': 'str'}
```

In this page: channel values ['email', 'form'], customer_plan values ['Basis', 'Plus']

## GET /tickets/{id}

```
{'id': 'str', 'body': 'str', 'channel': 'str', 'handled': 'null', 'subject': 'str', 'received_at': 'str', 'customer_plan': 'str'}
```
