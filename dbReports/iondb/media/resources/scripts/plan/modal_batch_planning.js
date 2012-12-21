TB.namespace('TB.plan.batchdownload');

TB.plan.batchdownload.ready = function() {
    $('#modal_batch_planning').on('hidden', function() {
        $('body #modal_batch_planning').remove();
    });
    $(function() {

        /*TYPE ERROR!!! $('#modalBatchPlanning').uniform is not a function
         $('#modalBatchPlanning').uniform({
         holder_class : 'control-group'
         , msg_selector: 'p.help-block'
         , error_class : 'alert alert-error'
         , prevent_submit      : true
         });
         */

        $(".submitDownload").click(function(e) {
            e.preventDefault();
            $('#modal_batch_planning').submit()
        });

        $('#modal_batch_planning').submit(function() {
            // validate input
            var planCountError = ""
            $('#planCountError').text(planCountError)
            var planCount = $('#planCount').val();
            var templateId = $('#modal_batch_planning #selectedTemplateId').val();

            console.log("planCount=", planCount, "templateId=", templateId);

            if (!planCount.match(/^[0-9]+$/) || (parseInt(planCount, 10) < 2)) {
                planCountError += 'Number of plans should be a whole number and be at least 2.';
            }

            if (planCountError) {
                $('#planCount').effect("highlight", {
                    "color" : "#F20C18"
                }, 2000);
                $('#planCountError').text(planCountError)
                return false;
            }

            var url = "/plan/template/" + templateId + "/planCount/" + planCount + "/getcsvforbatchplanning.csv/", type = $('#modal_batch_planning #modalBatchPlanning').attr('method');

            var data = {
                'format' : 'csv'
            };
            console.log(url);
            jQuery.download(url, data, 'POST');
            $('body #modal_batch_planning').modal("hide");
            return false;
        });

        function isValid() {
            var form = $('#modalBatchPlanning');
            var settings = form.uniform.defaults;
            form.find(settings.field_selector).each(function() {
                $(this).blur();
            });

            return !form.find('.' + settings.invalid_class).add('.' + settings.error_class).exists();
        }

    });
};
