def get_scenario_new_chart():
    return {
        "messages": [{"role": "user", "content": "Create a Helm chart for nginx web server"}]
    }

def get_scenario_update_chart():
    return {
        "messages": [{"role": "user", "content": "Update the replica count for nginx chart to 3"}]
    }
