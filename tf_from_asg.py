from argparse import ArgumentParser
import boto3
import base64
from jinja2 import Environment, BaseLoader, Template


DEFAULT_TEMPLATE = '''
module "{{ MODULE_NAME }}" {
  source = "../modules/autoscaling/qw_asg"

  key_name              = "${var.key_name}"
  asg_cluster           = "{{ ASG_CLUSTER }}"
  zones                 = "${var.default_zones}"
  asg_queue             = "{{ QUEUE_NAME }}"
  asg_name              = "{{ ASG_NAME }}"
  worker_security_group = ["${var.worker_security_group}"]
  consumer_config       = "{{ CONSUMER_CONFIG }}"
  r53_zone              = "${var.r53_zone}"
  hosted_domain         = "${var.hosted_domain}"
  vpc_subnet_ids        = ["${split(",", join(",", var.vpc_public_subnet_ids))}"]
  name_tag              = "${var.name_tag}"
  zookeeper_dns         = "${module.zookeeper.zookeeper_dns}"
  env                   = "${var.env}"
  app_branch            = "${var.app_branch}"
  asg_desired           = "{{ ASG_DESIRED }}"
  asg_max               = "{{ ASG_MAX }}"
  asg_min               = "{{ ASG_MIN }}"
}
'''

# TODO: They only differ in the final piece of the name, templatize better
LC_TEMPLATE = 'terraform import module.{}.aws_launch_configuration.qw-asg-launch-config {}'
ASG_TEMPLATE = 'terraform import module.{}.aws_autoscaling_group.qw-asg {}'

def main():
    parser = ArgumentParser()
    parser.add_argument('--template-file', default=None, dest='template_file')
    parser.add_argument('--asg-prefix', dest='prefix')
    parser.add_argument('--aws-env', default='klaviyo-dev', dest='aws_env')
    parser.add_argument('--output-file', default=None, dest='output_file')
    args = parser.parse_args()
    template_filename = args.template_file
    if template_filename:
        with open(args.template_file, 'r') as template_file:
            template = Template(template_file.read())
    else:
        template = Environment(loader=BaseLoader).from_string(DEFAULT_TEMPLATE)

    prefix = args.prefix
    client = boto3.session.Session(profile_name=args.aws_env).client('autoscaling')

    asgs_to_process = get_autoscaling_information(client, prefix)

    print 'Processing the following autoscaling groups:'
    print asgs_to_process
    # Item is the ASG Name
    # Value is the dict of collected information
    import_statements = []
    terraform_modules = []
    for asg_name, asg_info in asgs_to_process.iteritems():
        terraform_modules.append(generate_tf_for_asg(asg_info, template))
        import_statements.extend(import_statements_from_asg(asg_name, asg_info))

    if args.output_file:
        with open(args.output_file, 'w') as output_file:
            output_file.write('Generated Module Code:\n')
            output_file.writelines(terraform_modules)
            output_file.write('Generated Import Code:\n')
            output_file.writelines(import_statements)
    else:
        print 'Generated Module Code:\n'
        for module in terraform_modules:
            print module
        print 'Generated Import Code:\n'
        for import_statement in import_statements:
            print import_statement

def get_autoscaling_information(autoscaling_client, asg_name_prefix):
    # Don't clever up the place - iterate over all ASGs, for each which matches the prefix, pull the relevant data.
    # Later go and stitch in the LC sourced information
    asg_paginator = autoscaling_client.get_paginator('describe_auto_scaling_groups')
    asg_iterator = asg_paginator.paginate()
    asgs_to_process = {}
    for response in asg_iterator:
        for asg_response in response['AutoScalingGroups']:
            if asg_response['AutoScalingGroupName'].startswith(asg_name_prefix):
                asgs_to_process[asg_response['AutoScalingGroupName']] = {
                    'name' : asg_response['AutoScalingGroupName'],
                    'tags' : asg_response['Tags'],
                    'lc_name' : asg_response['LaunchConfigurationName'],
                    'asg_min' : asg_response['MinSize'],
                    'asg_max' : asg_response['MaxSize'],
                    'asg_desired' : asg_response['DesiredCapacity'],
                }

    lc_to_asg_name = {asg['lc_name'] : asg['name'] for asg in asgs_to_process.values()}
    lc_paginator = autoscaling_client.get_paginator('describe_launch_configurations')

    # Criminally inefficient. Should probably iterate over the LCs we actually want, but the paginator is limited to
    # 50 names when passing em in and I'd prefer not to implement my own pagination
    lc_iterator = lc_paginator.paginate()
    for lc_response in lc_iterator:
        for lc in lc_response['LaunchConfigurations']:
            if lc['LaunchConfigurationName'] not in lc_to_asg_name:
                continue

            asgs_to_process[lc_to_asg_name[lc['LaunchConfigurationName']]]['lc_info'] = get_launch_config_template_data_for_response(lc)

    return asgs_to_process

def get_launch_config_template_data_for_response(launch_configuration_response):
    # Take the LC Response info and rip out the consumer config.
    user_data = base64.b64decode(launch_configuration_response['UserData'])
    user_data = user_data.split('\n')
    for line in user_data:
        if 'CONSUMERS_CONFIGURATION' in line:
            return line[32:len(line)-1]

def generate_tf_for_asg(asg_info, template):
    # TODO: Template the TF bits from the ASG info we've pulled. Might not be perfect, but we can take a rough cut
    # TODO: Think about whether or not we can make this more reusable - right now it's heavily tied to the current qw module implementation
    dns_cluster_name = get_dns_safe_cluster_name(asg_info)
    cluster_name = get_cluster_name(asg_info)
    asg_context = {'MODULE_NAME' : cluster_name,
                   'ASG_CLUSTER' : dns_cluster_name,
                   'CONSUMER_CONFIG' : asg_info['lc_info'],
                   'ASG_NAME' : asg_info['name'],
                   'QUEUE_NAME' : get_queue_from_info(asg_info),
                   'ASG_MIN' : asg_info['asg_min'],
                   'ASG_MAX' : asg_info['asg_max'],
                   'ASG_DESIRED' : asg_info['asg_desired'],
                   }
    return template.render(**asg_context)

def get_queue_from_info(asg_info):
    for tag_response in asg_info['tags']:
        if tag_response['Key'] == 'queue':
            return tag_response['Value']

def import_statements_from_asg(asg_name, asg_info):
    # asg_info is asg_name -> dictionary of collected infor
    cluster_name = get_dns_safe_cluster_name(asg_info)
    return [LC_TEMPLATE.format(cluster_name, asg_info['lc_name']),
            ASG_TEMPLATE.format(cluster_name, asg_name)]

def get_dns_safe_cluster_name(asg_info):
    return asg_info['name'].replace('/', '-').replace('.', '-')

def get_cluster_name(asg_info):
    return asg_info['name'].replace('/', '_').replace('.', '_')

if __name__ == '__main__':
    main()